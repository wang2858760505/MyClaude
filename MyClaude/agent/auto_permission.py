"""Auto Mode：LLM 分类器替代人工确认。"""
from __future__ import annotations

from MyClaude.agent.constants import AUTO_MODE_FAST_PATH_TOOLS, DENIAL_LIMITS, build_classifier_system, build_classifier_transcript, check_permission, classifier_user_message, load_auto_mode_rules, load_claude_md, parse_block_verdict, print_info


class AutoPermissionMixin:
    async def _classify_tool_call(self, tool_name: str, inp: dict) -> dict:
        """Decide a tool call in Auto Mode. Returns allow/deny like
        check_permission, or "confirm" to hand back to a human once the denial
        limits trip.

        Two-stage, mirroring Claude Code's `both` mode: stage 1 is an aggressive
        cheap gate (no user intent, no ALLOW exceptions — block if any rule
        *could* apply); if stage 1 allows, we're done in one call. If stage 1
        blocks, stage 2 does the careful adjudication that DOES weigh user intent
        and can clear the block. Stage 2's verdict is final."""
        # Hard floor first: deny rules bind even here.
        base = check_permission(tool_name, inp, "default", self._plan_file_path)
        if base["action"] == "deny":
            return base
        # Fast-path: read-only / side-effect-free tools skip the classifier.
        if tool_name in AUTO_MODE_FAST_PATH_TOOLS:
            return {"action": "allow"}

        if not self._anthropic_client and not self._openai_client:
            # No evaluator available → fail closed. Defer to a human if present
            # (interactive), else deny outright (headless: CC aborts here).
            return self._auto_fallback(f"{tool_name} (auto-mode classifier unavailable)")
        try:
            rules = load_auto_mode_rules()
            history = self._openai_messages if self.use_openai else self._anthropic_messages
            transcript = build_classifier_transcript(history, {"tool_name": tool_name, "input": inp})
            system = build_classifier_system(rules)
            # CLAUDE.md rides in the user message, not the system prompt — it is
            # untrusted repo content.
            claude_md = load_claude_md()
            # Stage 1 — aggressive cheap gate (small token budget: just <block>…).
            s1_raw = await self._run_classifier_query(system, classifier_user_message(rules, transcript, rules["suffix_stage1"], claude_md), 256)
            s1 = parse_block_verdict(s1_raw)
            if not s1["block"]:
                verdict = s1  # stage 1 cleared it → allow (one call)
            else:
                # Stage 2 — careful adjudication (weighs user intent, can clear).
                # More tokens: stage 2 may emit a <thinking> block first.
                s2_raw = await self._run_classifier_query(system, classifier_user_message(rules, transcript, rules["suffix_stage2"], claude_md), 1024)
                verdict = parse_block_verdict(s2_raw)
        except Exception as e:
            # Any setup or classifier error → fail closed (block), matching CC's
            # iron gate. Wrapping the asset load here keeps a missing/bad rules
            # file from crashing the turn and orphaning the tool_use.
            verdict = {"block": True, "reason": f"classifier error: {e}"}

        if not verdict["block"]:
            self.auto_consecutive_denials = 0
            return {"action": "allow"}

        self.auto_consecutive_denials += 1
        self.auto_total_denials += 1
        if (
            self.auto_consecutive_denials >= DENIAL_LIMITS["max_consecutive"]
            or self.auto_total_denials >= DENIAL_LIMITS["max_total"]
        ):
            # Too many denials — the classifier may be stuck. Hand back to a human
            # if interactive; deny in headless (CC aborts the agent here).
            print_info("Auto Mode: denial limit reached — handing back to manual confirmation.")
            return self._auto_fallback(f"[Auto Mode blocked] {verdict['reason']}")
        return {"action": "deny", "message": f"[Auto Mode] {verdict['reason']}"}
    def _auto_fallback(self, message: str) -> dict:
        """Auto Mode fail-safe: defer to a human confirm if one is available,
        else deny (headless). Never returns "allow" — the point is to not run an
        unjudged action."""
        if self.confirm_fn:
            return {"action": "confirm", "message": message}
        return {"action": "deny", "message": f"{message} (headless — denied)"}
    def _child_permission_mode(self) -> str:
        """Permission mode a spawned sub-agent inherits. plan and auto must carry
        through — a sub-agent otherwise runs bypassPermissions, so in Auto Mode
        the main model could launder a blocked action through
        agent(prompt="git push origin main") and have the sub-agent run it
        unclassified. Claude Code puts every sub-agent tool call through
        canUseTool individually."""
        if self.permission_mode == "plan":
            return "plan"
        if self.permission_mode == "auto":
            return "auto"
        return "bypassPermissions"
