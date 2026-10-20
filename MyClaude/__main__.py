"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from .agent import Agent
from .providers import ProviderError, resolve_provider
from .ui import print_welcome, print_user_prompt, print_error, print_info, print_plan_for_approval, print_plan_approval_options
from .session import load_session, get_latest_session_id
from .memory import list_memories
from .skills import discover_skills, resolve_skill_prompt, get_skill_by_name, execute_skill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="myclaude",
        description="MyClaude — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--auto", action="store_true", help="Auto Mode: LLM classifier judges each action")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use (default: per provider)")
    parser.add_argument("--provider", "-p", default=None, help="LLM provider: deepseek/qwen/anthropic/openai/openai-responses (auto-detected from env)")
    parser.add_argument("--api-base", default=None, help="Override API base URL")
    parser.add_argument("--check", action="store_true", help="Provider health check: verify API key and endpoint reachability, then exit")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    if args.auto:
        return "auto"
    return "default"


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print("  Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        # Always signal a running /loop or /goal to stop — during its inter-tick
        # wait or between-turn evaluation the agent isn't "processing", so the
        # abort path below wouldn't catch it.
        agent.stop_loop()
        agent.stop_goal()
        # is_processing tracks the live task; _output_buffer is only set for
        # SUB-agents, so testing it here meant the main agent could never be
        # interrupted mid-task.
        if agent._aborted is False and agent.is_processing:
            # Agent is processing
            agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/goal" or inp.startswith("/goal "):
            condition = inp[len("/goal"):].strip()
            if not condition:
                agent.show_goal()
                continue
            directive = agent.set_goal(condition)
            try:
                await agent.pursue_goal(directive)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))
            continue
        if inp == "/loop" or inp.startswith("/loop "):
            rest = inp[len("/loop"):].strip()
            try:
                await agent.run_loop(rest)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .claude/skills/<name>/SKILL.md")
            else:
                print_info(f"{len(skills)} skills:")
                for s in skills:
                    tag = f"/{s.name}" if s.user_invocable else s.name
                    print(f"    {tag} ({s.source}) — {s.description}")
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        result = execute_skill(skill.name, cmd_args)
                        if result:
                            await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        resolved = resolve_skill_prompt(skill, cmd_args)
                        await agent.chat(resolved)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))

    # Loop exited (EOF / exit / quit) — release MCP subprocesses (issue #8)
    await agent.close()


def main() -> None:
    args = parse_args()

    if args.help:
        print("""
Usage: myclaude [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --auto              Auto Mode: an LLM classifier judges each action instead of asking
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: per provider)
  --provider, -p      LLM provider: deepseek / qwen / anthropic / openai / openai-responses
                      (default: auto-detected from DEEPSEEK_API_KEY / DASHSCOPE_API_KEY /
                       ANTHROPIC_API_KEY / OPENAI_API_KEY, in that order;
                       openai-responses 走 OpenAI Responses API，需显式指定)
  --api-base URL      Override API base URL (proxy / self-hosted endpoints)
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /goal <condition>   Pursue a goal across turns until an evaluator judges it met
  /goal               Show the active goal's status
  /loop [interval] <prompt>  Re-run a prompt on an interval (5m/2h) or self-paced
  /memory             List saved memories
  /skills             List available skills
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  myclaude "fix the bug in src/app.ts"
  myclaude --yolo "run all tests and fix failures"
  myclaude --plan "how would you refactor this?"
  myclaude --max-cost 0.50 --max-turns 20 "implement feature X"
  myclaude -p qwen -m qwen3.8-max "hello"        # 指定供应商与模型
  myclaude -p deepseek                            # 指定供应商，用其默认模型
  myclaude --api-base https://your-proxy/v1 -m your-model "hello"
  myclaude --resume
  myclaude  # starts interactive REPL
""")
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    api_base = args.api_base

    # ── 供应商解析（详见 providers.py）──
    # 优先级：--provider > MYCLAUDE_PROVIDER 环境变量 > 按 SEARCH_ORDER 自动探测
    try:
        resolved = resolve_provider(args.provider, api_base)
    except ProviderError as e:
        print_error(str(e))
        sys.exit(1)

    # ── 健康检查预检（--check）：验证 key + 端点连通性后退出，不进 REPL ──
    if args.check:
        import asyncio
        from .providers import check_provider_health
        result = asyncio.run(check_provider_health(resolved))
        latency = f"{result['latency_ms']:.0f}ms"
        if result["ok"]:
            print_info(f"Health check OK — {resolved.provider.name} ({latency}): {result['detail']}")
        else:
            print_error(f"Health check FAILED — {resolved.provider.name} ({latency}): {result['detail']}")
        sys.exit(0 if result["ok"] else 1)

    # 模型选择：--model > MYCLAUDE_MODEL > MINI_CLAUDE_MODEL（向后兼容）> 供应商默认
    model = (
        args.model
        or os.environ.get("MYCLAUDE_MODEL")
        or os.environ.get("MINI_CLAUDE_MODEL")
        or resolved.provider.default_model
    )

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved.base_url if resolved.use_openai else None,
        anthropic_base_url=None if resolved.use_openai else resolved.base_url,
        api_key=resolved.api_key,
        use_openai=resolved.use_openai,
        use_responses=resolved.protocol == "responses",
    )

    # Resume session
    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                agent.restore_session({
                    "anthropicMessages": session.get("anthropicMessages"),
                    "openaiMessages": session.get("openaiMessages"),
                })
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # One-shot mode — always release MCP subprocesses on the way out
        # (issue #8)
        async def _one_shot() -> None:
            try:
                await agent.chat(prompt)
            finally:
                await agent.close()
        try:
            asyncio.run(_one_shot())
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
