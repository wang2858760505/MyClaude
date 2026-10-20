"""Agent 包共享的常量、导入与模块级函数。"""

from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable
import anthropic
import openai
from ..tools import (
    tool_definitions,
    execute_tool,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    get_active_tool_definitions,
    ToolDef,
    PermissionMode,
    _truncate_result,
)
from ..memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from ..autonomy import (
    goal_directive,
    GOAL_EVALUATOR_SYSTEM,
    GOAL_TRANSCRIPT_FRAMING,
    goal_judge_user_message,
    parse_goal_verdict,
    GOAL_MAX_ITERATIONS,
    parse_loop_input,
    is_daily_wording,
    OFFER_CLOUD_THRESHOLD_SECONDS,
    SCHEDULE_WAKEUP_TOOL,
    clamp_wakeup_delay,
    dynamic_loop_directive,
    LOOP_MAX_ITERATIONS,
    load_auto_mode_rules,
    build_classifier_system,
    AUTO_MODE_FAST_PATH_TOOLS,
    DENIAL_LIMITS,
    build_classifier_transcript,
    parse_block_verdict,
    classifier_user_message,
)
from ..ui import (
    print_assistant_text,
    print_tool_call,
    print_tool_result,
    print_error,
    print_confirmation,
    print_divider,
    print_cost,
    print_retry,
    print_info,
    print_sub_agent_start,
    print_sub_agent_end,
    start_spinner,
    stop_spinner,
)
from ..session import save_session
from ..prompt import build_system_prompt, build_static_system_prompt, build_dynamic_system_context, build_user_context_reminder, load_claude_md
from ..subagent import get_sub_agent_config
from ..mcp_client import McpManager
def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False
async def _with_retry(fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)
MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}
def _get_context_window(model: str) -> int:
    return MODEL_CONTEXT.get(model, 200000)
def _model_supports_thinking(model: str) -> bool:
    m = model.lower()
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    return False
def _model_supports_adaptive_thinking(model: str) -> bool:
    m = model.lower()
    return "opus-4-6" in m or "sonnet-4-6" in m
def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384
def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]
def _to_responses_tools(tools: list[ToolDef]) -> list[dict]:
    """Chat Completions 把函数参数包在 function 字段里；Responses API 是
    扁平结构（type/name/description/parameters 平铺在顶层）。"""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in tools
    ]
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
SNIP_HOT_OVERRIDE = 0.75
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes
KEEP_RECENT_RESULTS = 3
