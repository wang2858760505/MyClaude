"""Responses API 协议适配层的单元测试。

覆盖：
  - providers: openai-responses 注册表条目与解析（protocol / use_openai 语义）
  - Agent 协议判定: use_responses 强制 use_openai=True（openai 族共享逻辑）
  - _to_responses_tools: 工具 schema 的扁平化转换
  - _openai_messages_to_responses_input: 本地规范化历史 → Responses input items
  - _responses_instructions: system 提示词槽位提取

全部离线运行，不发起任何网络请求。
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude.providers import REGISTRY, SEARCH_ORDER, ProviderError, resolve_provider
from MyClaude.agent import Agent
from MyClaude.agent.constants import _to_responses_tools


def make_responses_agent(**kw) -> Agent:
    """构造一个不联网的 Responses 协议 Agent（test-key 不会触发任何请求）。"""
    return Agent(api_key="test-key", use_responses=True, **kw)


class ResponsesProviderTests(unittest.TestCase):
    def tearDown(self):
        for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MYCLAUDE_PROVIDER"):
            os.environ.pop(var, None)

    def test_registry_entry(self):
        """注册表条目：protocol=responses，key 复用 OPENAI_API_KEY。"""
        p = REGISTRY["openai-responses"]
        self.assertEqual(p.protocol, "responses")
        self.assertEqual(p.api_key_env, "OPENAI_API_KEY")

    def test_not_in_search_order(self):
        """Responses API 是显式选择项（与 openai 共用同一把 key，
        自动探测永远轮不到它），不参与 SEARCH_ORDER。"""
        self.assertNotIn("openai-responses", SEARCH_ORDER)

    def test_explicit_resolution(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        r = resolve_provider("openai-responses")
        self.assertEqual(r.provider.name, "openai-responses")
        self.assertEqual(r.protocol, "responses")
        self.assertTrue(r.use_openai)  # 归入 openai 协议族

    def test_base_url_env_override(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_BASE_URL"] = "https://proxy.example/v1"
        r = resolve_provider("openai-responses")
        self.assertEqual(r.base_url, "https://proxy.example/v1")

    def test_unknown_provider_error_lists_all(self):
        """报错提示要包含 openai-responses（列出完整注册表）。"""
        with self.assertRaises(ProviderError) as ctx:
            resolve_provider("gpt9")
        self.assertIn("openai-responses", str(ctx.exception))


class ProtocolFamilyTests(unittest.TestCase):
    def test_use_responses_forces_openai_family(self):
        """use_responses 隐含 use_openai=True：压缩/会话/子代理等
        openai 分支对 Responses 协议原样生效。"""
        a = make_responses_agent()
        self.assertTrue(a.use_openai)
        self.assertTrue(a.use_responses)

    def test_client_init_uses_openai_sdk(self):
        a = make_responses_agent()
        self.assertIsNotNone(a._openai_client)
        self.assertIsNone(a._anthropic_client)
        # system 仍占据 messages[0] 槽位（plan 模式切换等逻辑依赖它），
        # 请求时由 _responses_instructions 提取走 instructions 参数
        self.assertEqual(a._openai_messages[0]["role"], "system")

    def test_default_protocol_is_chat_completions(self):
        a = Agent(api_key="test-key", use_openai=True)
        self.assertTrue(a.use_openai)
        self.assertFalse(a.use_responses)

    def test_anthropic_unaffected(self):
        a = Agent(api_key="test-key", use_openai=False)
        self.assertFalse(a.use_responses)
        self.assertFalse(a.use_openai)


class ResponsesToolsTests(unittest.TestCase):
    def test_flat_tool_format(self):
        tools = [{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        }]
        out = _to_responses_tools(tools)
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["name"], "read_file")
        self.assertEqual(out[0]["description"], "Read a file")
        self.assertEqual(out[0]["parameters"], {"type": "object", "properties": {}})
        # 扁平结构：不再有 chat-completions 的嵌套 function 字段
        self.assertNotIn("function", out[0])


class ResponsesConversionTests(unittest.TestCase):
    def _agent_with_history(self, messages) -> Agent:
        a = make_responses_agent()
        a._openai_messages = messages
        return a

    def test_system_goes_to_instructions_not_input(self):
        """system 消息不进入 input items，由 instructions 参数承载。"""
        a = self._agent_with_history([
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "hello"},
        ])
        items = a._openai_messages_to_responses_input()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0], {"role": "user", "content": "hello"})
        self.assertEqual(a._responses_instructions(), "You are a coding agent.")

    def test_tool_call_round_trip(self):
        """assistant.tool_calls → function_call item；
        role=tool → function_call_output item，ID 统一为 call_id。"""
        a = self._agent_with_history([
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": "Let me look.", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"file_path": "a.py"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
        ])
        items = a._openai_messages_to_responses_input()

        self.assertEqual(items[0], {"role": "user", "content": "read it"})
        self.assertEqual(items[1], {"role": "assistant", "content": "Let me look."})

        self.assertEqual(items[2]["type"], "function_call")
        self.assertEqual(items[2]["call_id"], "call_1")
        self.assertEqual(items[2]["name"], "read_file")
        self.assertEqual(items[2]["arguments"], '{"file_path": "a.py"}')

        self.assertEqual(items[3]["type"], "function_call_output")
        self.assertEqual(items[3]["call_id"], "call_1")
        self.assertEqual(items[3]["output"], "file body")

    def test_assistant_text_and_multiple_tool_calls(self):
        """一条 assistant 消息里的多个工具调用各生成一条 function_call，
        顺序与 tool_calls 数组一致。"""
        a = self._agent_with_history([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_files", "arguments": None}},
                {"id": "c2", "type": "function",
                 "function": {"name": "grep_search", "arguments": '{"pattern": "x"}'}},
            ]},
        ])
        items = a._openai_messages_to_responses_input()
        # content=None → 不产生文本 item
        self.assertEqual([it["type"] for it in items], ["function_call", "function_call"])
        self.assertEqual([it["call_id"] for it in items], ["c1", "c2"])
        # arguments 为 None → 兜底为 "{}"
        self.assertEqual(items[0]["arguments"], "{}")

    def test_instructions_fallback_without_system_message(self):
        """历史为空/无 system 时，instructions 兜底用实例 system prompt。"""
        a = make_responses_agent()
        a._openai_messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(a._responses_instructions(), a._system_prompt)


class _Item:
    """模拟 SDK 流事件/对象的轻量命名空间。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self._it = None

    def __aiter__(self):
        self._it = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResponses:
    """替换 AsyncOpenAI.responses，捕获请求参数并回放预设事件流。"""
    def __init__(self, events):
        self._events = events
        self.kwargs = None

    async def create(self, **kw):
        self.kwargs = kw
        return _FakeStream(self._events)


class ResponsesStreamTests(unittest.TestCase):
    """_call_responses_stream 的端到端归一化测试（mock SDK，不联网）。"""

    def _agent(self) -> Agent:
        a = make_responses_agent()
        a._openai_messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "hi"},
        ]
        a._output_buffer = []  # 捕获文本输出而非打印
        return a

    def test_text_and_tool_call_normalization(self):
        events = [
            _Item(type="response.output_text.delta", delta="Reading "),
            _Item(type="response.output_text.delta", delta="file."),
            _Item(type="response.output_item.added",
                  item=_Item(type="function_call", id="item_1",
                             call_id="call_9", name="read_file", arguments="")),
            _Item(type="response.function_call_arguments.delta",
                  item_id="item_1", delta='{"file'),
            _Item(type="response.function_call_arguments.delta",
                  item_id="item_1", delta='_path":"a.py"}'),
            _Item(type="response.completed",
                  response=_Item(usage=_Item(
                      input_tokens=100, output_tokens=20,
                      input_tokens_details=_Item(cached_tokens=60)))),
        ]
        a = self._agent()
        fake = _FakeResponses(events)
        a._openai_client.responses = fake

        result = asyncio.run(a._call_responses_stream())

        # 归一化输出与 chat-completions 形状一致 → _chat_openai 零改动复用
        msg = result["choices"][0]["message"]
        self.assertEqual(msg["content"], "Reading file.")
        self.assertEqual(msg["tool_calls"][0]["id"], "call_9")  # call_id 而非 item id
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"file_path":"a.py"}')
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["usage"],
                         {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 60})

        # 请求参数：system 走 instructions，input 不含 system，工具为扁平格式
        self.assertEqual(fake.kwargs["instructions"], "sys prompt")
        self.assertEqual(fake.kwargs["input"], [{"role": "user", "content": "hi"}])
        self.assertEqual(fake.kwargs["store"], False)
        self.assertEqual(fake.kwargs["tools"][0]["name"], "read_file")
        self.assertNotIn("function", fake.kwargs["tools"][0])

        # 流式文本也进了输出缓冲（供 run_once 捕获）
        self.assertIn("Reading file.", "".join(a._output_buffer))

    def test_pure_text_response(self):
        events = [
            _Item(type="response.output_text.delta", delta="hello"),
            _Item(type="response.completed",
                  response=_Item(usage=_Item(
                      input_tokens=10, output_tokens=5,
                      input_tokens_details=_Item(cached_tokens=0)))),
        ]
        a = self._agent()
        a._openai_client.responses = _FakeResponses(events)

        result = asyncio.run(a._call_responses_stream())
        msg = result["choices"][0]["message"]
        self.assertEqual(msg["content"], "hello")
        self.assertIsNone(msg["tool_calls"])
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_failure_event_raises(self):
        """response.failed 等终止事件必须抛错，不能静默吞掉。"""
        events = [_Item(type="response.failed", response=_Item(status="failed"))]
        a = self._agent()
        a._openai_client.responses = _FakeResponses(events)
        with self.assertRaises(RuntimeError):
            asyncio.run(a._call_responses_stream())


if __name__ == "__main__":
    unittest.main()
