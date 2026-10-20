"""Tool-call instrumentation — hooks, audit log, and result caching.

三个能力共用 execute_tool 这个唯一入口（execute_tool_checked 包装器）：

  1. Hooks  — PreToolUse / PostToolUse 拦截点，两层：
       - Python 注册表：register_hook(event, fn)，fn(name, inp) 返回非 None
         字符串即拦截（供测试与编程扩展）
       - settings.json 配置的 shell 命令（镜像 Claude Code 的 hooks 设计）：
           .claude/settings.json → {"hooks": {"PreToolUse": [{"command": "...", "timeout": 10}]}}
         约定：stdin 收到 JSON {tool_name, tool_input, session_id}；
         exit 0 = 放行；exit 2 = 拦截（stderr 作为拒绝理由回填给模型）；
         其他退出码/超时 = 非阻塞错误（放行，与 Claude Code 一致 fail-open）
  2. Audit  — 每次工具调用落一条 JSONL（~/.myclaude/audit/{session}.jsonl），
         监控与调试的数据源；MYCLAUDE_DEBUG=1 时同步打终端
  3. Cache  — 幂等只读工具的结果缓存：TTL 内直接复用。
         失效策略（双保险）：read_file 命中前校验文件 mtime；
         任何写类工具（write/edit/shell）执行后整表清空。
         Error 结果与超大结果不缓存。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from .tools import execute_tool, _load_settings

# ─── 配置 ────────────────────────────────────────────────────

HOOK_EVENTS = ("PreToolUse", "PostToolUse")

# 可缓存的幂等只读工具。web_fetch 虽只读但外部页面随时会变，不缓存。
CACHEABLE_TOOLS = {"read_file", "list_files", "grep_search"}
CACHE_TTL_S = 300            # 缓存存活 5 分钟
CACHE_MAX_ENTRY_CHARS = 50_000  # 超大结果不缓存（落盘机制已另行处理）

DEBUG = os.environ.get("MYCLAUDE_DEBUG") == "1"

# ─── 模块级状态 ──────────────────────────────────────────────

_registry: dict[str, list] = {e: [] for e in HOOK_EVENTS}
_cached_shell_hooks: dict[str, list[dict]] | None = None

_tool_cache: dict[str, tuple[float, str, float | None]] = {}
# key → (expires_at, result, file_mtime_or_None)；mtime 用于 read_file 命中校验


# ─── Python 注册表钩子 ───────────────────────────────────────


def register_hook(event: str, fn) -> None:
    """注册一个进程内钩子。fn(tool_name, inp) -> str|None，
    返回非 None 字符串表示拦截，字符串作为拒绝理由。"""
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unknown hook event: {event}. Valid: {HOOK_EVENTS}")
    _registry[event].append(fn)


def unregister_hook(event: str, fn) -> None:
    if fn in _registry.get(event, []):
        _registry[event].remove(fn)


def clear_hooks() -> None:
    """清空注册表（测试隔离用）。"""
    for k in _registry:
        _registry[k] = []


# ─── settings.json shell 钩子 ────────────────────────────────


def load_shell_hooks() -> dict[str, list[dict]]:
    """从 user + project 两级 settings.json 合并 hooks 配置，
    项目级后加载（同 permission rules 的优先级约定）。"""
    global _cached_shell_hooks
    if _cached_shell_hooks is not None:
        return _cached_shell_hooks

    merged: dict[str, list[dict]] = {e: [] for e in HOOK_EVENTS}
    user_settings = _load_settings(Path.home() / ".claude" / "settings.json")
    project_settings = _load_settings(Path.cwd() / ".claude" / "settings.json")
    for settings in [user_settings, project_settings]:
        if not settings or "hooks" not in settings:
            continue
        for event, entries in settings["hooks"].items():
            if event in HOOK_EVENTS and isinstance(entries, list):
                merged[event].extend(
                    e for e in entries if isinstance(e, dict) and e.get("command")
                )

    _cached_shell_hooks = merged
    return merged


def reset_hook_cache() -> None:
    global _cached_shell_hooks
    _cached_shell_hooks = None


async def _run_shell_hooks(event: str, tool_name: str, inp: dict, session_id: str) -> str | None:
    """跑某事件下配置的所有 shell 钩子。返回拦截理由或 None（放行）。
    exit 2 = 拦截；0 = 放行；其他 = 非阻塞错误（放行）。"""
    for entry in load_shell_hooks().get(event, []):
        payload = json.dumps({
            "tool_name": tool_name,
            "tool_input": inp,
            "session_id": session_id,
        })
        timeout = float(entry.get("timeout", 10))
        try:
            proc = await asyncio.create_subprocess_shell(
                entry["command"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(payload.encode()), timeout=timeout
            )
        except (asyncio.TimeoutError, OSError) as e:
            print(f"[hooks] {event} hook error (non-blocking): {e}", flush=True)
            continue
        if proc.returncode == 2:
            return stderr.decode(errors="replace").strip() or f"Blocked by {event} hook"
        # returncode 0 → 放行；其他码 → 非阻塞错误，继续
    return None


# ─── 审计日志 ────────────────────────────────────────────────


def _audit_dir() -> Path:
    d = Path.home() / ".myclaude" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compact_for_audit(value, limit: int = 2000) -> object:
    """审计输入时截断超长字段值（write_file 的 content 等），保持 JSONL 轻量。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _compact_for_audit(v, limit) for k, v in value.items()}
    return value


def log_audit(record: dict, session_id: str) -> None:
    """追加一条审计记录。失败静默（审计绝不能反过来弄垮工具执行）。"""
    try:
        path = _audit_dir() / f"{session_id or 'adhoc'}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if DEBUG:
            print(f"  [audit] {record['tool']} {record['duration_ms']:.0f}ms cached={record['cached']}", flush=True)
    except Exception:
        pass


def read_audit(session_id: str) -> list[dict]:
    """读回某会话的全部审计记录（调试/回放用）。"""
    path = _audit_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ─── 工具结果缓存 ────────────────────────────────────────────


def _cache_key(tool_name: str, inp: dict) -> str:
    try:
        normalized = json.dumps(inp, sort_keys=True, ensure_ascii=False)
    except Exception:
        normalized = str(inp)
    return f"{tool_name}:{normalized}"


def _file_mtime(path: str | None) -> float | None:
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def cache_get(tool_name: str, inp: dict) -> str | None:
    """TTL 内且（对 read_file）文件 mtime 未变才命中。"""
    key = _cache_key(tool_name, inp)
    entry = _tool_cache.get(key)
    if entry is None:
        return None
    expires_at, result, cached_mtime = entry
    if time.monotonic() > expires_at:
        _tool_cache.pop(key, None)
        return None
    if tool_name == "read_file":
        current = _file_mtime(inp.get("file_path"))
        if current != cached_mtime:
            _tool_cache.pop(key, None)
            return None
    return result


def cache_put(tool_name: str, inp: dict, result: str) -> None:
    if tool_name not in CACHEABLE_TOOLS or len(result) > CACHE_MAX_ENTRY_CHARS:
        return
    if result.startswith("Error"):
        return
    key = _cache_key(tool_name, inp)
    mtime = _file_mtime(inp.get("file_path")) if tool_name == "read_file" else None
    if len(_tool_cache) > 128:  # 简单容量上限，防止长会话无限增长
        _tool_cache.clear()
    _tool_cache[key] = (time.monotonic() + CACHE_TTL_S, result, mtime)


def cache_invalidate_all() -> None:
    """任何写类工具执行后调用：目录内容与文件都不可信，整表清空。"""
    _tool_cache.clear()


# ─── 统一入口包装器 ──────────────────────────────────────────


async def execute_tool_checked(
    name: str,
    inp: dict,
    read_file_state: dict[str, float] | None = None,
    session_id: str = "",
) -> str:
    """execute_tool 的插桩包装：PreToolUse 钩子 → 缓存查询 → 真实执行
    （写后失效缓存）→ PostToolUse 钩子 → 审计落盘。钩子/审计/缓存的
    任何异常都不影响工具执行本身（fail-open，与权限系统的 deny-only
    拦截形成互补：钩子做增量管控，权限做底线管控）。"""
    started = time.monotonic()
    denied: str | None = None
    cached = False

    # ① PreToolUse：Python 钩子（先）+ shell 钩子（后）
    try:
        for fn in _registry["PreToolUse"]:
            denied = fn(name, inp)
            if denied:
                break
        if not denied:
            denied = await _run_shell_hooks("PreToolUse", name, inp, session_id)
    except Exception as e:
        print(f"[hooks] PreToolUse error (non-blocking): {e}", flush=True)

    if denied:
        result = f"Blocked by PreToolUse hook: {denied}"
        log_audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": session_id, "tool": name,
            "input": _compact_for_audit(inp),
            "duration_ms": (time.monotonic() - started) * 1000,
            "result_chars": len(result), "error": False,
            "cached": False, "hook_denied": denied,
        }, session_id)
        return result

    # ② 缓存查询（命中也要过 PostToolUse，保持事件流完整）
    hit = cache_get(name, inp)
    if hit is not None:
        result = hit
        cached = True
    else:
        result = await execute_tool(name, inp, read_file_state)
        cache_put(name, inp, result)
        # 写类工具执行过 → 目录状态不可信，整表失效
        if name in ("write_file", "edit_file", "run_shell"):
            cache_invalidate_all()

    # ③ PostToolUse（只通知，无拦截语义）
    try:
        for fn in _registry["PostToolUse"]:
            fn(name, inp, result)
        await _run_shell_hooks("PostToolUse", name, inp, session_id)
    except Exception as e:
        print(f"[hooks] PostToolUse error (non-blocking): {e}", flush=True)

    # ④ 审计
    log_audit({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": session_id, "tool": name,
        "input": _compact_for_audit(inp),
        "duration_ms": (time.monotonic() - started) * 1000,
        "result_chars": len(result),
        "error": result.startswith("Error") or result.startswith("Blocked"),
        "cached": cached,
        "hook_denied": None,
    }, session_id)
    return result
