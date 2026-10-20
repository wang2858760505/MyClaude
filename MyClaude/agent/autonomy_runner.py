"""/goal 与 /loop 自主模式的执行循环。"""
from __future__ import annotations

from MyClaude.agent.constants import GOAL_EVALUATOR_SYSTEM, GOAL_MAX_ITERATIONS, GOAL_TRANSCRIPT_FRAMING, LOOP_MAX_ITERATIONS, OFFER_CLOUD_THRESHOLD_SECONDS, SCHEDULE_WAKEUP_TOOL, asyncio, clamp_wakeup_delay, dynamic_loop_directive, goal_directive, goal_judge_user_message, is_daily_wording, parse_goal_verdict, parse_loop_input, print_info, time


class AutonomyRunnerMixin:
    def set_goal(self, condition: str) -> str:
        """Set the active goal and return the first-turn directive to run."""
        self.active_goal = {"condition": condition, "iterations": 0, "started_at": time.time(), "last_reason": None}
        print_info(f'◎ /goal active — Stop hook condition: "{condition}"')
        return goal_directive(condition)
    def show_goal(self) -> None:
        """`/goal` with no argument prints the current goal's status."""
        if not self.active_goal:
            print_info("No active goal. Set one with /goal <condition>.")
            return
        secs = time.time() - self.active_goal["started_at"]
        last = f"\n  last reason: {self.active_goal['last_reason']}" if self.active_goal["last_reason"] else ""
        print_info(
            f"◎ /goal active\n  condition: {self.active_goal['condition']}\n"
            f"  iterations: {self.active_goal['iterations']}\n  elapsed: {secs:.1f}s{last}"
        )
    async def pursue_goal(self, directive: str) -> None:
        """Pursue the active goal: run the directive turn, then loop
        evaluate → (not met) feed reason back → next turn, until met, impossible,
        budget/iteration cap, or interrupt."""
        if not self.active_goal:
            return
        self.goal_stop = False
        try:
            await self.chat(directive)
            # Evaluate the turn that just finished *before* any cap or next-turn
            # decision, so the final turn's output is never left unjudged.
            while self.active_goal and not self.goal_stop and not self._aborted:
                verdict = await self._evaluate_goal(self.active_goal["condition"])
                if verdict["ok"]:
                    turns = self.active_goal["iterations"] + 1
                    secs = time.time() - self.active_goal["started_at"]
                    plural = "" if turns == 1 else "s"
                    print_info(f"✓ Goal achieved ({turns} turn{plural}, {secs:.1f}s): {verdict['reason']}")
                    break
                if verdict.get("impossible"):
                    print_info(f"Hooks: Prompt hook condition judged impossible: {verdict['reason']}")
                    break

                # Not met: record and decide whether another turn is allowed.
                self.active_goal["iterations"] += 1
                self.active_goal["last_reason"] = verdict["reason"]
                print_info(f"Hooks: Prompt hook condition was not met: {verdict['reason']}")

                budget = self._check_budget()
                if budget["exceeded"]:
                    print_info(f"Goal stopped: {budget['reason']}")
                    break
                # Hard ceiling regardless of --max-turns: --max-turns only counts
                # tool-executing turns (_check_budget), so a no-tool goal loop
                # needs an unconditional backstop of its own.
                if self.active_goal["iterations"] >= GOAL_MAX_ITERATIONS:
                    print_info(f"Goal stopped: reached {GOAL_MAX_ITERATIONS} iterations without meeting the condition.")
                    break
                if self.goal_stop or self._aborted:
                    break

                await self.chat(
                    f"Hooks: Prompt hook condition was not met: {verdict['reason']}\n\nKeep working toward the goal."
                )
            if self.goal_stop or self._aborted:
                print_info("Goal pursuit interrupted.")
        finally:
            # Clear on any exit (met / impossible / capped / interrupted) so a
            # stale goal never lingers. Real Claude Code keeps it session-scoped
            # and resumable; we don't implement resume.
            self.active_goal = None
    async def _evaluate_goal(self, condition: str) -> dict:
        """One evaluator pass over the just-finished turn's transcript. The
        transcript is sent as its own assistant message (framed by a preceding
        user message as data-to-judge), so a crafted turn can't smuggle fake
        user/judge text into the evaluator's context — real Claude Code likewise
        sends the turn as a separate transcript message, not inlined."""
        transcript = self._extract_last_assistant_text()
        messages = [
            {"role": "user", "content": GOAL_TRANSCRIPT_FRAMING},
            {"role": "assistant", "content": transcript or "(no assistant output)"},
            {"role": "user", "content": goal_judge_user_message(condition)},
        ]
        try:
            raw = await self._run_evaluator_query(GOAL_EVALUATOR_SYSTEM, messages)
            return parse_goal_verdict(raw)
        except Exception as e:
            # Evaluator error → treat as not-met (never accidentally clears goal).
            return {"ok": False, "reason": f"evaluator error: {e}", "impossible": False}
    async def _run_evaluator_query(self, system: str, messages: list) -> str:
        """Send a role-separated evaluator query on whichever backend is
        configured and return the model's text. Like _build_side_query but takes
        a full messages array (that one is single-user-message, for memory
        recall)."""
        if self._anthropic_client:
            resp = await self._anthropic_client.messages.create(
                model=self.model, max_tokens=512, system=system, temperature=0, messages=messages,
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        if self._openai_client:
            resp = await self._openai_client.chat.completions.create(
                model=self.model, max_tokens=512, temperature=0,
                messages=[{"role": "system", "content": system}, *messages],
            )
            return resp.choices[0].message.content or "" if resp.choices else ""
        raise RuntimeError("no evaluator model available")
    async def _run_classifier_query(self, system: str, user: str, max_tokens: int) -> str:
        """Single-message classifier query with a caller-chosen max_tokens, so the
        two Auto Mode stages can size their budgets differently (stage 1 is a tiny
        gate, stage 2 has room to think). temperature=0 for a deterministic
        verdict, matching Claude Code's classifier."""
        if self._anthropic_client:
            resp = await self._anthropic_client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system, temperature=0,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        if self._openai_client:
            resp = await self._openai_client.chat.completions.create(
                model=self.model, max_tokens=max_tokens, temperature=0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            return resp.choices[0].message.content or "" if resp.choices else ""
        raise RuntimeError("no classifier model available")
    def _extract_last_assistant_text(self) -> str:
        """The text of the most recent assistant turn, for the evaluator to
        judge. Transcript-only: the action under judgement is the latest turn."""
        if self.use_openai:
            for m in reversed(self._openai_messages):
                if m.get("role") == "assistant" and isinstance(m.get("content"), str):
                    return m["content"]
            return ""
        for m in reversed(self._anthropic_messages):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        return ""
    async def run_loop(self, raw_input: str) -> None:
        """Entry point for the /loop command. Parses the input, then drives the
        matching mode. Returns without looping if the input is malformed."""
        spec = parse_loop_input(raw_input)
        if "error" in spec:
            print_info(spec["error"])
            return
        # Offer-cloud decision point (interval >=60min or daily wording). Real
        # Claude Code asks whether to convert to a persistent cloud schedule that
        # survives the session; this teaching CLI has no cloud, so we only
        # surface it.
        wants_cloud = (
            (spec["mode"] == "interval" and spec["interval_seconds"] >= OFFER_CLOUD_THRESHOLD_SECONDS)
            or is_daily_wording(raw_input)
        )
        if wants_cloud:
            print_info(
                "(Real Claude Code would offer to convert this to a persistent cloud schedule "
                "that keeps running after the session ends. This teaching build has no cloud "
                "backend — continuing in-session.)"
            )

        self.loop_stop = False
        try:
            if spec["mode"] == "interval":
                await self._run_loop_interval(spec)
            else:
                await self._run_loop_dynamic(spec)
        except asyncio.CancelledError:
            print_info("Loop interrupted.")
    async def _run_loop_interval(self, spec: dict) -> None:
        """Interval mode: re-run the prompt every N seconds until interrupted or
        the iteration cap. Corresponds to Claude Code's in-session CronCreate
        path (session-only, not persisted). We use a plain timer in place of the
        cron engine + KAIROS daemon."""
        print_info(
            f"⟳ /loop scheduled every {spec['interval_label']} (session-only, not persisted — "
            "dies when this process exits). Ctrl+C to stop."
        )
        iterations = 0
        while not self.loop_stop and not self._aborted:
            iterations += 1
            print_info(f"⟳ loop tick {iterations}")
            await self.chat(spec["prompt"])

            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Loop stopped: {budget['reason']}")
                break
            # --max-turns also bounds loop ticks: _check_budget's turn counter
            # only increments on tool-executing turns, so a plain-text loop would
            # never hit it — treat --max-turns as a tick limit here too.
            if self.max_turns is not None and iterations >= self.max_turns:
                print_info(f"Loop stopped: tick limit reached ({iterations} >= {self.max_turns}).")
                break
            if iterations >= LOOP_MAX_ITERATIONS:
                print_info(f"Loop stopped: reached {LOOP_MAX_ITERATIONS} ticks.")
                break
            interrupted = await self._interruptible_sleep(spec["interval_seconds"])
            if interrupted:
                print_info("Loop stopped.")
                break
    async def _run_loop_dynamic(self, spec: dict) -> None:
        """Dynamic mode: run the tick, then let the main model self-pace via
        schedule_wakeup. If it scheduled a wakeup, wait the (clamped) delay and
        run again with the prompt it passed back; if it didn't, the loop has
        converged. Faithful to "dynamic pacing is decided by the main model, no
        separate evaluator." schedule_wakeup is exposed only for the loop."""
        print_info(
            "⟳ /loop dynamic (self-paced) — the model schedules its own next run, or ends the "
            "loop. Ctrl+C to stop."
        )
        had_tool = any(t["name"] == "schedule_wakeup" for t in self.tools)
        if not had_tool:
            self.tools = self.tools + [SCHEDULE_WAKEUP_TOOL]
        self.schedule_wakeup_enabled = True
        prompt = spec["prompt"]
        iterations = 0
        try:
            while not self.loop_stop and not self._aborted:
                iterations += 1
                self.pending_wakeup = None
                await self.chat(dynamic_loop_directive(prompt))

                if not self.pending_wakeup:
                    plural = "" if iterations == 1 else "s"
                    print_info(f"⟳ Loop converged after {iterations} tick{plural} (model scheduled no wakeup).")
                    break
                budget = self._check_budget()
                if budget["exceeded"]:
                    print_info(f"Loop stopped: {budget['reason']}")
                    break
                if self.max_turns is not None and iterations >= self.max_turns:
                    print_info(f"Loop stopped: tick limit reached ({iterations} >= {self.max_turns}).")
                    break
                if iterations >= LOOP_MAX_ITERATIONS:
                    print_info(f"Loop stopped: reached {LOOP_MAX_ITERATIONS} ticks.")
                    break
                delay = self.pending_wakeup["delay_seconds"]
                print_info(f"⟳ next run in {delay}s — {self.pending_wakeup['reason']}")
                prompt = self.pending_wakeup["prompt"] or prompt
                interrupted = await self._interruptible_sleep(delay)
                if interrupted:
                    print_info("Loop stopped.")
                    break
        finally:
            # Remove schedule_wakeup so it isn't exposed outside the dynamic loop.
            if not had_tool:
                self.tools = [t for t in self.tools if t["name"] != "schedule_wakeup"]
            self.schedule_wakeup_enabled = False
            self.pending_wakeup = None
    def _execute_schedule_wakeup(self, inp: dict) -> str:
        """schedule_wakeup executor: record the requested wakeup for the loop
        driver. Delay is clamped to [60, 3600]; the driver reads pending_wakeup
        after the turn converges."""
        delay = clamp_wakeup_delay(inp.get("delaySeconds"))
        reason = inp.get("reason") if isinstance(inp.get("reason"), str) else ""
        prompt = inp.get("prompt") if isinstance(inp.get("prompt"), str) else ""
        self.pending_wakeup = {"delay_seconds": delay, "reason": reason, "prompt": prompt}
        return f"Wakeup scheduled in {delay}s. The loop will resume then; end your turn now."
    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep that resolves early (returning True) if the loop is stopped or
        the turn is aborted. Avoids blocking on a long interval past a Ctrl+C."""
        import time as _time
        start = _time.time()
        while _time.time() - start < seconds:
            if self.loop_stop or self._aborted:
                return True
            await asyncio.sleep(min(0.2, seconds))
        return False
    def stop_loop(self) -> None:
        """Stop a running /loop (called from the REPL's interrupt handler)."""
        self.loop_stop = True
    def stop_goal(self) -> None:
        """Stop a running /goal pursuit (called from the REPL's interrupt
        handler). Takes effect at the next turn boundary — an in-flight turn is
        aborted separately via abort()."""
        self.goal_stop = True
