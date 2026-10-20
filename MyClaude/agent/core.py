"""Agent 核心 — 对话循环、双后端 LLM 调用、工具执行分发。

职责方法按功能分散在 6 个 mixin 模块中（压缩 / 预算 / 自主模式 /
权限分类 / 特殊工具 / 会话持久化），本文件保留类的主干。
"""
from __future__ import annotations

from MyClaude.agent.compression import CompressionMixin
from MyClaude.agent.budget import BudgetMixin
from MyClaude.agent.autonomy_runner import AutonomyRunnerMixin
from MyClaude.agent.auto_permission import AutoPermissionMixin
from MyClaude.agent.special_tools import SpecialToolsMixin
from MyClaude.agent.session_store import SessionStoreMixin
from MyClaude.hooks import execute_tool_checked
from MyClaude.agent.constants import AUTO_MODE_FAST_PATH_TOOLS, Any, Awaitable, CONCURRENCY_SAFE_TOOLS, Callable, McpManager, MemoryPrefetch, ToolDef, _get_context_window, _get_max_output_tokens, _model_supports_adaptive_thinking, _model_supports_thinking, _to_openai_tools, _to_responses_tools, _with_retry, anthropic, asyncio, build_dynamic_system_context, build_static_system_prompt, build_user_context_reminder, check_permission, execute_tool, format_memories_for_injection, get_active_tool_definitions, json, openai, os, print_assistant_text, print_confirmation, print_cost, print_divider, print_info, print_tool_call, print_tool_result, start_memory_prefetch, start_spinner, stop_spinner, time, tool_definitions, uuid


class Agent(CompressionMixin, BudgetMixin, AutonomyRunnerMixin, AutoPermissionMixin, SpecialToolsMixin, SessionStoreMixin):

    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "claude-opus-4-6",
        api_base: str | None = None,
        anthropic_base_url: str | None = None,
        api_key: str | None = None,
        use_openai: bool | None = None,
        use_responses: bool = False,
        thinking: bool = False,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
    ):
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        # 协议判定：use_responses 是 openai 协议族的特例（同一 SDK 客户端、
        # 同一套本地消息历史，仅线上请求格式不同），因此它强制 use_openai=True，
        # 让压缩/会话/子代理等所有 openai 分支原样生效。显式 use_openai 优先；
        # 未指定时 api_base 非空即视为 OpenAI 兼容端点（子代理等内部调用走这条路径）
        self.use_responses = bool(use_responses)
        if self.use_responses:
            self.use_openai = True
        else:
            self.use_openai = bool(api_base) if use_openai is None else use_openai
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self.effective_window = _get_context_window(model) - 20000
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0       # Prompt-cache hits (billed ~0.1x)
        self.total_cache_creation_tokens = 0   # Prompt-cache writes (billed ~1.25x)
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0.0

        # /goal — session-scoped Stop-hook condition, pursued across turns
        self.active_goal: dict | None = None
        self.goal_stop = False  # set on interrupt to break out of goal pursuit

        # /loop dynamic mode — set when the model calls schedule_wakeup during a
        # tick, read (and cleared) by the loop driver after the turn converges.
        self.pending_wakeup: dict | None = None
        self.loop_stop = False  # set on interrupt to break out of a running loop
        # schedule_wakeup is routed to the internal executor only while a dynamic
        # loop is active, so it can't shadow a same-named tool or be reached out
        # of band.
        self.schedule_wakeup_enabled = False

        # Auto Mode — transcript-classifier denial tracking (DENIAL_LIMITS).
        self.auto_consecutive_denials = 0
        self.auto_total_denials = 0

        # Abort support
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # Permission whitelist
        self._confirmed_paths: set[str] = set()

        # Plan mode state
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        self._context_cleared: bool = False  # Set when plan approval clears context

        # Thinking mode
        self._thinking_mode = self._resolve_thinking_mode()

        # Output buffer (sub-agents capture output)
        self._output_buffer: list[str] | None = None

        # Read-before-edit: track file read timestamps (absolutePath → mtime)
        self._read_file_state: dict[str, float] = {}

        # MCP integration
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # Memory recall state — semantic prefetch per user turn. The handle
        # lives on the instance so a recall that settles after this turn's
        # last API call is carried over and injected next turn (issue #7).
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0
        self._memory_prefetch: MemoryPrefetch | None = None

        # Separate message histories
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []

        # Build system prompt with a static/dynamic split for prefix caching.
        # A custom system prompt overrides both halves (all of it is treated as
        # static). Otherwise the static core is cacheable, env/git/skills form
        # the dynamic tail, and CLAUDE.md + date go into the FIRST user message
        # as a <system-reminder> (Claude Code's prependUserContext) — see chat.
        # Keeping project-specific content out of the system maximizes sharing.
        self._user_context_reminder = ""
        if custom_system_prompt:
            self._static_system_prompt = custom_system_prompt
            self._dynamic_system_context = ""
        else:
            self._static_system_prompt = build_static_system_prompt()
            self._dynamic_system_context = build_dynamic_system_context()
            self._user_context_reminder = build_user_context_reminder()
        self._base_system_prompt = (
            self._static_system_prompt + "\n\n" + self._dynamic_system_context
            if self._dynamic_system_context else self._static_system_prompt
        )
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        # Optional: cap the SDK's own retry layer (default 2). Set
        # MYCLAUDE_SDK_MAX_RETRIES=0 to isolate our _with_retry in tests.
        _sdk_retries: dict[str, Any] = {}
        _rv = os.environ.get("MYCLAUDE_SDK_MAX_RETRIES")
        if _rv is not None and _rv != "":
            try:
                _sdk_retries["max_retries"] = int(_rv)
            except ValueError:
                pass
        # Initialize clients
        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key, **_sdk_retries)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            kwargs.update(_sdk_retries)
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None
    @staticmethod
    def _block_to_dict(block) -> dict:
        """Convert an Anthropic content block to a plain dict for storage."""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input) if hasattr(block.input, 'items') else block.input}
        # Fallback
        return {"type": block.type}
    def _build_anthropic_system(self) -> list[dict]:
        """Build the `system` field as text blocks with a cache_control
        breakpoint on the static core. Everything up to and including that block
        (the tool schemas render before `system`, so they are covered too) is
        cached server-side; the dynamic tail sits after the breakpoint. This is
        Claude Code's scope-omitted path. See how-claude-code-works ch3.6."""
        plan_suffix = self._build_plan_mode_prompt() if self.permission_mode == "plan" else ""
        dynamic_text = (self._dynamic_system_context + plan_suffix).strip()
        blocks: list[dict] = [
            {"type": "text", "text": self._static_system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        if dynamic_text:
            blocks.append({"type": "text", "text": dynamic_text})
        return blocks
    def _build_side_query(self):
        """Build a sideQuery callable for memory recall and the Auto Mode
        classifier, works with both backends. temperature=0 for a deterministic
        decision — the same input should always yield the same verdict (Claude
        Code runs the classifier at temperature 0)."""
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system: str, user_message: str) -> str:
                resp = await client.messages.create(
                    model=model, max_tokens=256, system=system, temperature=0,
                    messages=[{"role": "user", "content": user_message}],
                )
                return "".join(b.text for b in resp.content if b.type == "text")
            return _sq
        if self._openai_client and self.use_responses:
            # Responses API 版 side query：instructions 承载 system，输入是
            # 纯文本；store=False 保持无状态（与主循环的本地历史自治一致）
            client = self._openai_client
            model = self.model
            async def _sq_resp(system: str, user_message: str) -> str:
                resp = await client.responses.create(
                    model=model, max_output_tokens=256, temperature=0,
                    instructions=system, input=user_message, store=False,
                )
                return resp.output_text or ""
            return _sq_resp
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_oai(system: str, user_message: str) -> str:
                resp = await client.chat.completions.create(
                    model=model, max_tokens=256, temperature=0,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                )
                return resp.choices[0].message.content or "" if resp.choices else ""
            return _sq_oai
        return None
    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        """Stream an Anthropic API call. When a tool_use content block finishes
        during streaming, on_tool_block_complete fires immediately so the caller
        can start execution before the full response arrives (streaming tool
        execution -- inspired by Claude Code's content_block_stop streaming pattern)."""
        async def _do():
            max_output = _get_max_output_tokens(self.model)
            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else 16384,
                "system": self._build_anthropic_system(),
                "tools": get_active_tool_definitions(self.tools),
                # Rolling message-array cache breakpoint, applied to a copy so
                # the persistent history stays free of cache_control metadata.
                "messages": self._with_cache_breakpoints(self._anthropic_messages),
            }

            if self._thinking_mode in ("adaptive", "enabled"):
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True
            # Track in-flight tool_use blocks by index for streaming execution
            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue

                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                            self._emit_text(delta.text)
                        elif hasattr(delta, 'thinking'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n  [thinking] ")
                                first_text = False
                            self._emit_text(delta.thinking)
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json

                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            import json as _json
                            try:
                                parsed = _json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            on_tool_block_complete({
                                "type": "tool_use", "id": tb["id"],
                                "name": tb["name"], "input": parsed,
                            })

                final_message = await stream.get_final_message()

            # Filter out thinking blocks
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        return await _with_retry(_do)
    async def _call_openai_stream(self) -> dict:
        async def _do():
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                max_tokens=16384,
                tools=_to_openai_tools(get_active_tool_definitions(self.tools)),
                messages=self._openai_messages,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(delta.content)
                    content += delta.content

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                        else:
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)
    async def _call_stream_normalized(self) -> dict:
        """OpenAI 协议族请求分派：Chat Completions 与 Responses API 在线上
        格式不同，但本地都维护同一套 chat-completions 形状的规范化历史。
        本方法选择对应协议的调用路径；两条路径都把结果归一化成
        _chat_openai 消费的 {"choices":[...], "usage":{...}} 形状，因此
        权限/工具执行/压缩逻辑对协议无感知。"""
        if self.use_responses:
            return await self._call_responses_stream()
        return await self._call_openai_stream()
    def _openai_messages_to_responses_input(self) -> list[dict]:
        """把本地规范化历史（chat-completions 形状）翻译成 Responses API 的
        input items。映射关系：
          messages[0] role=system  → 不进入 input（走 instructions 参数）
          role=user                → {"role":"user","content": str}
          role=assistant           → 文本 message item + 每个工具调用一条
                                     {"type":"function_call", call_id, name, arguments}
          role=tool                → {"type":"function_call_output", call_id, output}
        工具调用 ID 两侧不同名：chat 侧叫 tool_calls[].id / tool_call_id，
        Responses 侧统一为 call_id。"""
        items: list[dict] = []
        for m in self._openai_messages:
            role = m.get("role")
            if role == "system":
                continue
            if role == "user":
                items.append({"role": "user", "content": m.get("content", "")})
            elif role == "assistant":
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id"),
                    "output": m.get("content", ""),
                })
        return items
    def _responses_instructions(self) -> str:
        """system 提示词走 Responses API 的独立 instructions 参数。本地历史
        仍把 system 存在 messages[0]（plan 模式切换等逻辑依赖这个槽位），
        请求时从这里取出；历史为空时兜底用实例上的完整 system prompt。"""
        if self._openai_messages and self._openai_messages[0].get("role") == "system":
            return self._openai_messages[0].get("content", "")
        return self._system_prompt
    async def _call_responses_stream(self) -> dict:
        """OpenAI Responses API 流式调用。设计要点：
        - store=False：无状态模式，服务端不保存会话，每轮重发完整 input——
          与本地历史自治（压缩管线会改写历史）严格兼容；previous_response_id
          需要服务端存储，与压缩管线互斥，故不采用。
        - 工具调用经 response.output_item.added（拿到 call_id/name）注册，
          再由 response.function_call_arguments.delta 增量拼接 arguments。
        - 归一化输出与 _call_openai_stream 完全同形，工具执行层零感知。"""
        async def _do():
            stream = await self._openai_client.responses.create(
                model=self.model,
                instructions=self._responses_instructions(),
                input=self._openai_messages_to_responses_input(),
                tools=_to_responses_tools(get_active_tool_definitions(self.tools)),
                max_output_tokens=16384,
                stream=True,
                store=False,
            )

            content = ""
            first_text = True
            tool_calls: dict[str, dict] = {}   # item_id → 累积中的调用
            item_order: list[str] = []         # 保持 output_item.added 的到达顺序
            usage = None

            async for event in stream:
                etype = getattr(event, "type", "")

                if etype == "response.output_text.delta":
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(event.delta)
                    content += event.delta

                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        item_id = item.id
                        tool_calls[item_id] = {
                            # call_id 是工具结果回填时要用的 ID（对应 chat 侧的 tc id）
                            "id": getattr(item, "call_id", None) or item_id,
                            "name": getattr(item, "name", "") or "",
                            "arguments": getattr(item, "arguments", "") or "",
                        }
                        item_order.append(item_id)

                elif etype == "response.function_call_arguments.delta":
                    tc = tool_calls.get(event.item_id)
                    if tc is not None:
                        tc["arguments"] += event.delta

                elif etype == "response.completed":
                    u = getattr(event.response, "usage", None)
                    if u is not None:
                        details = getattr(u, "input_tokens_details", None)
                        usage = {
                            "prompt_tokens": u.input_tokens,
                            "completion_tokens": u.output_tokens,
                            # Responses API 的缓存命中在 input_tokens_details 上
                            "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
                        }

                elif etype in ("response.failed", "response.error", "response.incomplete"):
                    raise RuntimeError(f"Responses API stream {etype}: {getattr(event, 'error', None) or getattr(event, 'response', None)}")

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tool_calls[i]["id"], "type": "function",
                     "function": {"name": tool_calls[i]["name"], "arguments": tool_calls[i]["arguments"]}}
                    for i in item_order
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)
    async def _chat_anthropic(self, user_message: str) -> None:
        self._push_anthropic_user_message(user_message)
        # Auto-compact at turn boundary only — the last message is now plain
        # user text, so the slice in _compact_anthropic won't sever a
        # tool_use ↔ tool_result pair from the previous turn's tool execution.
        await self._check_and_compact()

        # Memory prefetch: drain carry-over, then start fresh (issue #7)
        self._start_memory_prefetch_for_turn(user_message, self._anthropic_messages)

        while True:
            if self._aborted:
                break

            self._run_compression_pipeline()

            # Consume memory prefetch if settled (non-blocking poll, zero-wait)
            self._consume_memory_prefetch_if_ready(self._anthropic_messages)

            if not self.is_sub_agent:
                start_spinner()

            # ── Streaming tool execution ──────────────────────────────
            # As each tool_use content block completes during streaming, check
            # if it's concurrency-safe and auto-allowed. If so, start execution
            # immediately — the tool runs while the model still generates.
            early_executions: dict[str, asyncio.Task] = {}

            def _on_tool_block(block: dict):
                # In Auto Mode, only fast-path (classifier-exempt) tools may start
                # early — otherwise a concurrency-safe-but-classified tool (e.g.
                # web_fetch) would run before the classifier ever sees it.
                if self.permission_mode == "auto" and block["name"] not in AUTO_MODE_FAST_PATH_TOOLS:
                    return
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    perm = check_permission(block["name"], block["input"], self.permission_mode, self._plan_file_path)
                    if perm["action"] == "allow":
                        task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task

            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()
            # Anthropic reports cached tokens separately: `input_tokens` counts
            # only the uncached prefix, while cache_read/cache_creation are
            # billed at 0.1x/1.25x. Track them apart for cost.
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            self.total_input_tokens += response.usage.input_tokens
            self.total_cache_read_tokens += cache_read
            self.total_cache_creation_tokens += cache_creation
            self.total_output_tokens += response.usage.output_tokens
            # Estimate next-turn context size for the compaction gauge: the full
            # prompt we just sent (input + cache_read + cache_creation) plus the
            # output we just generated, which becomes part of the next request.
            self.last_input_token_count = (
                response.usage.input_tokens + cache_read + cache_creation + response.usage.output_tokens
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            self._anthropic_messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            if not tool_uses:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens, self.total_cache_read_tokens, self.total_cache_creation_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                # Every tool_use needs a paired tool_result or the message
                # history is invalid for the next API call. Pair each pending
                # call with a refusal instead of silently dropping it.
                for task in early_executions.values():
                    task.cancel()
                self._anthropic_messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu.id,
                     "content": f"Tool call not executed: {budget['reason']}"}
                    for tu in tool_uses
                ]})
                break

            # Process tools: early-started ones (from streaming) just await
            # their result; others go through permission check + execution.
            tool_results: list[dict] = []
            context_break = False
            for tu in tool_uses:
                if context_break or self._aborted:
                    break
                inp = dict(tu.input) if hasattr(tu.input, 'items') else tu.input
                print_tool_call(tu.name, inp)

                # Was this tool already started during streaming?
                early_task = early_executions.get(tu.id)
                if early_task:
                    raw = await early_task
                    res = self._persist_large_result(tu.name, raw)
                    print_tool_result(tu.name, res)
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                    continue

                # Permission check for tools not started early. Auto Mode routes
                # through the transcript classifier; other modes use static rules.
                if self.permission_mode == "auto":
                    perm = await self._classify_tool_call(tu.name, inp)
                else:
                    perm = check_permission(tu.name, inp, self.permission_mode, self._plan_file_path)
                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message"):
                    # Auto Mode confirms carry a reason, not a path — never cache
                    # them, or one approval would whitelist every later action
                    # with the same reason.
                    cacheable = self.permission_mode != "auto"
                    if not cacheable or perm["message"] not in self._confirmed_paths:
                        confirmed = await self._confirm_dangerous(perm["message"])
                        if not confirmed:
                            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "User denied this action."})
                            continue
                        if cacheable:
                            self._confirmed_paths.add(perm["message"])

                raw = await self._execute_tool_call(tu.name, inp)
                res = self._persist_large_result(tu.name, raw)
                print_tool_result(tu.name, res)

                if self._context_cleared:
                    self._context_cleared = False
                    # History was just cleared — route through the helper so the
                    # rebuilt context's first user message carries the reminder.
                    self._push_anthropic_user_message(res)
                    context_break = True
                    break
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

            if not context_break and tool_results:
                self._anthropic_messages.append({"role": "user", "content": tool_results})
            self._context_cleared = False
    async def _chat_openai(self, user_message: str) -> None:
        self._push_openai_user_message(user_message)
        # Auto-compact at turn boundary only — see _chat_anthropic for rationale.
        # The last message is now plain user text, so the slice in
        # _compact_openai won't orphan a tool_calls / tool message pair.
        await self._check_and_compact()

        # Memory prefetch: drain carry-over, then start fresh (issue #7)
        self._start_memory_prefetch_for_turn(user_message, self._openai_messages)

        while True:
            if self._aborted:
                break

            self._run_compression_pipeline()

            # Consume memory prefetch if settled (non-blocking poll, zero-wait)
            self._consume_memory_prefetch_if_ready(self._openai_messages)

            if not self.is_sub_agent:
                start_spinner()

            response = await self._call_stream_normalized()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                # OpenAI-compatible providers cache prefixes automatically; the
                # cached portion is included in prompt_tokens, so split it out to
                # avoid double-counting. Clamp to [0, prompt_tokens] since
                # compatible gateways don't guarantee the field. NOTE: priced at
                # Anthropic's 0.1x for simplicity; actual cached rates vary by
                # provider (OpenAI ~0.5x, gateways vary), so the estimate may be
                # off in either direction.
                prompt = response["usage"]["prompt_tokens"] or 0
                cached_oa = min(max(response["usage"].get("cached_tokens", 0) or 0, 0), prompt)
                completion = response["usage"]["completion_tokens"]
                self.total_input_tokens += prompt - cached_oa
                self.total_cache_read_tokens += cached_oa
                self.total_output_tokens += completion
                # Estimate next-turn context size: this prompt + the output we
                # just generated (which becomes part of the next request).
                self.last_input_token_count = prompt + completion

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            self._openai_messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens, self.total_cache_read_tokens, self.total_cache_creation_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                # Same pairing requirement as the Anthropic path: every
                # tool_call needs a role="tool" response.
                for tc in tool_calls:
                    if tc.get("id"):
                        self._openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"Tool call not executed: {budget['reason']}",
                        })
                break

            # Phase 1: Parse & permission-check (serial)
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if self._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                print_tool_call(fn_name, inp)

                if self.permission_mode == "auto":
                    perm = await self._classify_tool_call(fn_name, inp)
                else:
                    perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)
                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message"):
                    # Auto Mode confirms carry a reason, not a path — never cache
                    # them, or one approval would whitelist every later action
                    # with the same reason.
                    cacheable = self.permission_mode != "auto"
                    if not cacheable or perm["message"] not in self._confirmed_paths:
                        confirmed = await self._confirm_dangerous(perm["message"])
                        if not confirmed:
                            oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": "User denied this action."})
                            continue
                        if cacheable:
                            self._confirmed_paths.add(perm["message"])
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: Group & execute (parallel for consecutive safe tools)
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or self._aborted:
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                        raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                        res = self._persist_large_result(ct_item["fn"], raw)
                        print_tool_result(ct_item["fn"], res)
                        return ct_item, res

                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                    for ct_item, res in results:
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]})
                            continue
                        raw = await self._execute_tool_call(ct["fn"], ct["inp"])
                        res = self._persist_large_result(ct["fn"], raw)
                        print_tool_result(ct["fn"], res)

                        if self._context_cleared:
                            self._context_cleared = False
                            # History was just cleared — route through the helper
                            # so the first user message carries the reminder.
                            self._push_openai_user_message(res)
                            oai_context_break = True
                            break
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})

            self._context_cleared = False
    def _clear_history_keep_system(self) -> None:
        """Clear history but keep system prompt (used for clear-context plan approval)."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0
    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
    def _consume_memory_prefetch_if_ready(self, messages: list) -> None:
        """Inject recalled memories if the prefetch settled (non-blocking).
        Appends to the last user message to keep user/assistant alternation."""
        pf = self._memory_prefetch
        if not (pf and pf.settled and not pf.consumed):
            return
        pf.consumed = True
        try:
            memories = pf.task.result()
            if not memories:
                return
            injection_text = format_memories_for_injection(memories)
            last = messages[-1] if messages else None
            if last and last.get("role") == "user":
                content = last.get("content", "")
                if isinstance(content, str) or content is None:
                    last["content"] = (content or "") + "\n\n" + injection_text
                elif isinstance(content, list):
                    content.append({"type": "text", "text": injection_text})
            else:
                messages.append({"role": "user", "content": injection_text})
            for m in memories:
                self._already_surfaced_memories.add(m.path)
                self._session_memory_bytes += len(m.content.encode())
        except Exception:
            pass  # prefetch errors already logged
    def _emit_text(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)
    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        if name == "schedule_wakeup":
            # Only the internal dynamic-loop driver may route here; outside a
            # dynamic loop the tool isn't exposed, and this guard keeps a stray
            # call (or a same-named external tool) from reaching the executor.
            if not self.schedule_wakeup_enabled:
                return "schedule_wakeup is only available during /loop dynamic mode."
            return self._execute_schedule_wakeup(inp)
        # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        # 插桩包装：Pre/PostToolUse 钩子 + 审计 JSONL + 只读工具结果缓存
        return await execute_tool_checked(name, inp, self._read_file_state, session_id=self.session_id)

    def _push_anthropic_user_message(self, content: str) -> None:
        """Push a user message, prepending the CLAUDE.md/date <system-reminder>
        when it is the first user message of a (possibly just-cleared) context —
        Claude Code's prependUserContext, kept out of the cached system prompt.
        Embedded in the user message rather than a standalone message to preserve
        user/assistant alternation. Also used by the plan clear-and-execute path,
        which rebuilds history from empty."""
        if not self._anthropic_messages and self._user_context_reminder:
            self._anthropic_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": self._user_context_reminder},
                    {"type": "text", "text": content},
                ],
            })
        else:
            self._anthropic_messages.append({"role": "user", "content": content})
    def _push_openai_user_message(self, content: str) -> None:
        is_first_user = not any(m.get("role") == "user" for m in self._openai_messages)
        if is_first_user and self._user_context_reminder:
            self._openai_messages.append({"role": "user", "content": f"{self._user_context_reminder}\n\n{content}"})
        else:
            self._openai_messages.append({"role": "user", "content": content})
    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not _model_supports_thinking(self.model):
            return "disabled"
        if _model_supports_adaptive_thinking(self.model):
            return "adaptive"
        return "enabled"
    def _start_memory_prefetch_for_turn(self, user_message: str, messages: list) -> None:
        """Drain any carry-over prefetch from the previous turn (a recall
        that settled after the last API call would otherwise be dropped —
        issue #7), then start a fresh prefetch for this query."""
        self._consume_memory_prefetch_if_ready(messages)
        if self.is_sub_agent:
            return
        if self._memory_prefetch and not self._memory_prefetch.settled:
            self._memory_prefetch.task.cancel()
        sq = self._build_side_query()
        if sq:
            self._memory_prefetch = start_memory_prefetch(
                user_message, sq,
                self._already_surfaced_memories, self._session_memory_bytes,
            )
    def _with_cache_breakpoints(self, messages: list[dict]) -> list[dict]:
        """Return a COPY of the message list with a cache_control breakpoint on
        the last message's final content block, so every prior turn stays in the
        cached prefix and only the newest messages are processed. Pure: the
        persistent history is never mutated with this API metadata (Claude Code
        clones request params at the render layer for the same reason, keeping
        session save / compact / restore clean). Faithful to CC's
        assistantMessageToMessageParam, we look only at the very LAST block and
        skip it when it is a thinking block (unstable content → hurts cache
        hits). Only 1 message breakpoint + 1 system breakpoint per request."""
        if not messages:
            return messages
        out = list(messages)
        last = out[-1]
        raw = last.get("content")
        content = [{"type": "text", "text": raw}] if isinstance(raw, str) else list(raw)
        tail = content[-1] if content else None
        if isinstance(tail, dict) and tail.get("type") not in ("thinking", "redacted_thinking"):
            content[-1] = {**tail, "cache_control": {"type": "ephemeral"}}
            out[-1] = {**last, "content": content}
        return out
    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
    async def chat(self, user_message: str) -> None:
        # Lazily connect to MCP servers on first chat (main agent only)
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                print(f"[mcp] Init failed: {e}", flush=True)

        self._aborted = False
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.current_task()
        try:
            await coro
        except asyncio.CancelledError:
            self._aborted = True
        finally:
            self._current_task = None
        if not self.is_sub_agent:
            print_divider()
            self._auto_save()
    def clear_history(self) -> None:
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        self.last_input_token_count = 0
        print_info("Conversation cleared.")
    async def close(self) -> None:
        """Release external resources (MCP subprocesses) for a clean exit —
        see issue #8."""
        if self._mcp_initialized:
            await self._mcp_manager.disconnect_all()
    @property
    def is_processing(self) -> bool:
        return self._current_task is not None and not self._current_task.done()
    async def run_once(self, prompt: str) -> dict:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }
    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn
    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn
    def toggle_plan_mode(self) -> str:
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info(f"Exited plan mode → {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"
