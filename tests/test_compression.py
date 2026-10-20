"""4 层上下文压缩管线的单元测试。

管线（_run_compression_pipeline）按序执行三层，全部只做消息列表的就地改写：
  Tier 1 budget   —— 超预算的单条 tool_result 掐头去尾
  Tier 2 snip     —— 陈旧/重复的 tool_result 整条替换为占位符（缓存热时跳过）
  Tier 3 microcompact —— 空闲 5 分钟后，保留最近 N 条、清掉更老的

测试直接构造 Agent 实例（不联网）+ 手工布置消息列表，断言改写结果。
anthropic 与 openai 两种消息形态都测。
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude.agent import Agent
from MyClaude.agent.constants import (
    KEEP_RECENT_RESULTS,
    MICROCOMPACT_IDLE_S,
    SNIP_PLACEHOLDER,
    SNIP_THRESHOLD,
)

WIN = 100_000  # effective_window


def make_agent(**kw) -> Agent:
    """构造一个不联网的 Agent（test-key 不会触发任何请求）。"""
    a = Agent(api_key="test-key", **kw)
    a.effective_window = WIN
    a.last_input_token_count = 0
    a.last_api_call_time = 0  # 缓存冷
    return a


def anth_tool_result(rid: str, content: str) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": rid, "content": content}]}


def anth_tool_use(rid: str, name: str, file_path: str = "a.py") -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": rid, "name": name, "input": {"file_path": file_path}}]}


def set_utilization(a: Agent, ratio: float):
    a.last_input_token_count = int(WIN * ratio)


class BudgetTests(unittest.TestCase):
    def test_below_half_utilization_noop(self):
        a = make_agent()
        set_utilization(a, 0.4)
        msg = anth_tool_result("t1", "x" * 40_000)
        a._anthropic_messages = [msg]
        a._budget_tool_results_anthropic()
        self.assertEqual(len(msg["content"][0]["content"]), 40_000)

    def test_mid_utilization_uses_30k_budget(self):
        a = make_agent()
        set_utilization(a, 0.6)  # 0.5 ~ 0.7 → budget 30000
        original = "y" * 40_000
        msg = anth_tool_result("t1", original)
        a._anthropic_messages = [msg]
        a._budget_tool_results_anthropic()
        out = msg["content"][0]["content"]
        self.assertIn("[... budgeted:", out)
        self.assertLess(len(out), 40_000)

    def test_high_utilization_uses_15k_budget(self):
        a = make_agent()
        set_utilization(a, 0.8)  # > 0.7 → budget 15000
        msg = anth_tool_result("t1", "y" * 40_000)
        a._anthropic_messages = [msg]
        a._budget_tool_results_anthropic()
        out = msg["content"][0]["content"]
        self.assertLess(len(out), 20_000)

    def test_openai_variant(self):
        a = make_agent()
        set_utilization(a, 0.6)
        msg = {"role": "tool", "content": "z" * 40_000}
        a._openai_messages = [msg]
        a._budget_tool_results_openai()
        self.assertIn("[... budgeted:", msg["content"])


class SnipTests(unittest.TestCase):
    def _history(self, results: list[tuple[str, str, str]]) -> list[dict]:
        """results: [(tool_use_id, tool_name, file_path)] → [assistant*5, user*5]"""
        msgs = []
        for rid, name, fp in results:
            msgs.append(anth_tool_use(rid, name, fp))
        for rid, name, fp in results:
            msgs.append(anth_tool_result(rid, "content-of-" + rid))
        return msgs

    def test_cold_cache_high_utilization_snips_oldest(self):
        a = make_agent()
        set_utilization(a, SNIP_THRESHOLD)  # 恰好达到阈值
        a._anthropic_messages = self._history([
            ("t1", "read_file", "a.py"),
            ("t2", "read_file", "b.py"),
            ("t3", "read_file", "c.py"),
            ("t4", "read_file", "d.py"),
            ("t5", "read_file", "e.py"),
        ])
        a._snip_stale_results_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages if m["role"] == "user"]
        # KEEP_RECENT_RESULTS=3 → 最老的 2 条被 snip
        self.assertEqual(contents[0], SNIP_PLACEHOLDER)
        self.assertEqual(contents[1], SNIP_PLACEHOLDER)
        self.assertNotEqual(contents[2], SNIP_PLACEHOLDER)

    def test_hot_cache_blocks_snip_below_override(self):
        """缓存热 + 利用率未到 SNIP_HOT_OVERRIDE → 完全不动（保护缓存前缀）。"""
        a = make_agent()
        set_utilization(a, 0.6)  # 超过 SNIP_THRESHOLD 但 < 0.75
        a.last_api_call_time = time.time()  # 刚刚调用过 → 缓存热
        a._anthropic_messages = self._history([
            ("t1", "read_file", "a.py"), ("t2", "read_file", "b.py"),
            ("t3", "read_file", "c.py"), ("t4", "read_file", "d.py"),
            ("t5", "read_file", "e.py"),
        ])
        a._snip_stale_results_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages if m["role"] == "user"]
        self.assertTrue(all(c != SNIP_PLACEHOLDER for c in contents))

    def test_hot_cache_overridden_by_extreme_utilization(self):
        """利用率超过 SNIP_HOT_OVERRIDE 时，缓存热也照 snip（宁重建缓存不撑爆窗口）。"""
        a = make_agent()
        set_utilization(a, 0.9)
        a.last_api_call_time = time.time()
        a._anthropic_messages = self._history([
            ("t1", "read_file", "a.py"), ("t2", "read_file", "b.py"),
            ("t3", "read_file", "c.py"), ("t4", "read_file", "d.py"),
            ("t5", "read_file", "e.py"),
        ])
        a._snip_stale_results_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages if m["role"] == "user"]
        self.assertEqual(contents[0], SNIP_PLACEHOLDER)

    def test_duplicate_file_reads_dedupe(self):
        """同一文件读多次：旧的那几条 snip，保留最新一条。"""
        a = make_agent()
        set_utilization(a, SNIP_THRESHOLD)
        # 4 条结果：a, a, c, d —— t1/t2 读同一个文件
        a._anthropic_messages = self._history([
            ("t1", "read_file", "same.py"),
            ("t2", "read_file", "same.py"),
            ("t3", "read_file", "c.py"),
            ("t4", "read_file", "d.py"),
        ])
        a._snip_stale_results_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages if m["role"] == "user"]
        self.assertEqual(contents[0], SNIP_PLACEHOLDER)       # 旧的 same.py 副本被 snip
        self.assertNotEqual(contents[1], SNIP_PLACEHOLDER)    # 最新的 same.py 保留
        self.assertNotEqual(contents[3], SNIP_PLACEHOLDER)    # 落在 KEEP 窗口内的不动

    def test_openai_variant(self):
        a = make_agent()
        set_utilization(a, SNIP_THRESHOLD)
        a._openai_messages = [
            {"role": "tool", "content": "old-1"},
            {"role": "tool", "content": "old-2"},
            {"role": "tool", "content": "old-3"},
            {"role": "tool", "content": "old-4"},
            {"role": "tool", "content": "old-5"},
        ]
        a._snip_stale_results_openai()
        self.assertEqual(a._openai_messages[0]["content"], SNIP_PLACEHOLDER)
        self.assertNotEqual(a._openai_messages[4]["content"], SNIP_PLACEHOLDER)


class MicrocompactTests(unittest.TestCase):
    def _results(self, n: int) -> list[dict]:
        return [anth_tool_result(f"t{i}", f"content-{i}") for i in range(n)]

    def test_idle_clears_old_keeps_recent(self):
        a = make_agent()
        a.last_api_call_time = time.time() - MICROCOMPACT_IDLE_S - 10  # 空闲超时
        a._anthropic_messages = self._results(5)
        a._microcompact_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages]
        self.assertEqual(contents[0], "[Old result cleared]")
        self.assertEqual(contents[1], "[Old result cleared]")
        self.assertNotEqual(contents[2], "[Old result cleared]")  # 最近 3 条保留

    def test_recent_api_call_blocks_microcompact(self):
        a = make_agent()
        a.last_api_call_time = time.time()  # 刚调用 → 未空闲
        a._anthropic_messages = self._results(5)
        a._microcompact_anthropic()
        contents = [m["content"][0]["content"] for m in a._anthropic_messages]
        self.assertTrue(all(c != "[Old result cleared]" for c in contents))


class FindToolUseTests(unittest.TestCase):
    def test_found_by_id(self):
        a = make_agent()
        a._anthropic_messages = [anth_tool_use("tu_1", "grep_search", "x/")]
        info = a._find_tool_use_by_id("tu_1")
        self.assertEqual(info["name"], "grep_search")

    def test_not_found_returns_none(self):
        a = make_agent()
        self.assertIsNone(a._find_tool_use_by_id("missing"))


if __name__ == "__main__":
    unittest.main()
