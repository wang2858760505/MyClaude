"""hooks.py 插桩层的单元测试：钩子、审计日志、工具结果缓存。

覆盖：
  - Python 注册表钩子：PreToolUse 拦截 / PostToolUse 通知
  - shell 钩子：exit 2 拦截（stderr 回填）、exit 0 放行、非零非 2 放行
  - 结果缓存：TTL 命中、read_file mtime 失效、写类工具执行后整表清空、
    Error 结果不缓存、非缓存工具不缓存
  - 审计：拦截/命中/错误/钩子拒绝各产生一条 JSONL，超长输入截断
  - execute_tool_checked 端到端（真实 FS，临时目录）

全部离线；shell 钩子用 sys.executable 跑内联脚本，跨平台。
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude import hooks
from MyClaude.hooks import (
    cache_get,
    cache_invalidate_all,
    cache_put,
    clear_hooks,
    execute_tool_checked,
    reset_hook_cache,
)


class HooksTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        clear_hooks()
        reset_hook_cache()
        cache_invalidate_all()
        # 审计写到临时目录
        self._audit_patcher = mock.patch.object(hooks, "_audit_dir", return_value=self.tmp / "audit")
        self._audit_patcher.start()
        (self.tmp / "audit").mkdir()

    def tearDown(self):
        self._audit_patcher.stop()
        clear_hooks()
        reset_hook_cache()
        cache_invalidate_all()
        self._tmp.cleanup()

    def _audit_lines(self) -> list[dict]:
        path = self.tmp / "audit" / "s1.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


class PythonHookTests(HooksTestBase):
    def test_pre_hook_can_deny(self):
        hooks.register_hook("PreToolUse", lambda name, inp: "no shell for you" if name == "run_shell" else None)

        result = asyncio.run(execute_tool_checked("run_shell", {"command": "echo hi"}, session_id="s1"))

        self.assertIn("Blocked by PreToolUse hook", result)
        self.assertIn("no shell for you", result)
        records = self._audit_lines()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["hook_denied"], "no shell for you")

    def test_pre_hook_allow_and_post_hook_observes(self):
        seen = []
        hooks.register_hook("PostToolUse", lambda name, inp, result: seen.append((name, result)))

        result = asyncio.run(execute_tool_checked(
            "write_file", {"file_path": str(self.tmp / "a.txt"), "content": "hello"}, session_id="s1"))

        self.assertFalse(result.startswith("Error"))
        self.assertEqual(seen, [("write_file", result)])

    def test_hook_exception_is_fail_open(self):
        def boom(name, inp):
            raise RuntimeError("hook bug")
        hooks.register_hook("PreToolUse", boom)

        result = asyncio.run(execute_tool_checked(
            "write_file", {"file_path": str(self.tmp / "b.txt"), "content": "x"}, session_id="s1"))

        self.assertFalse(result.startswith("Error"))  # 钩子崩了不影响执行


class ShellHookTests(HooksTestBase):
    def _patch_shell_hooks(self, entries):
        return mock.patch.object(hooks, "load_shell_hooks",
                                 return_value={"PreToolUse": entries, "PostToolUse": []})

    def test_exit2_denies_with_stderr_reason(self):
        script = f'{sys.executable} -c "import sys; sys.stderr.write(\'forbidden\'); sys.exit(2)"'
        with self._patch_shell_hooks([{"command": script, "timeout": 15}]):
            result = asyncio.run(execute_tool_checked("run_shell", {"command": "echo hi"}, session_id="s1"))
        self.assertIn("Blocked by PreToolUse hook", result)
        self.assertIn("forbidden", result)

    def test_exit0_allows(self):
        script = f'{sys.executable} -c "import sys; sys.exit(0)"'
        with self._patch_shell_hooks([{"command": script, "timeout": 15}]):
            result = asyncio.run(execute_tool_checked(
                "write_file", {"file_path": str(self.tmp / "c.txt"), "content": "ok"}, session_id="s1"))
        self.assertFalse(result.startswith("Error"))
        self.assertFalse(result.startswith("Blocked"))

    def test_other_exit_code_is_fail_open(self):
        script = f'{sys.executable} -c "import sys; sys.exit(1)"'
        with self._patch_shell_hooks([{"command": script, "timeout": 15}]):
            result = asyncio.run(execute_tool_checked(
                "write_file", {"file_path": str(self.tmp / "d.txt"), "content": "ok"}, session_id="s1"))
        self.assertFalse(result.startswith("Blocked"))

    def test_stdin_receives_payload(self):
        # 钩子把 stdin 原样写文件，验证 payload 结构
        out = self.tmp / "payload.json"
        script = (
            f'{sys.executable} -c "import sys, pathlib; '
            f"pathlib.Path(r'{out}').write_bytes(sys.stdin.buffer.read()); sys.exit(0)\""
        )
        with self._patch_shell_hooks([{"command": script, "timeout": 15}]):
            asyncio.run(execute_tool_checked("grep_search", {"pattern": "x"}, session_id="s1"))
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["tool_name"], "grep_search")
        self.assertEqual(payload["session_id"], "s1")
        self.assertIn("tool_input", payload)


class CacheTests(HooksTestBase):
    def _write(self, name: str, content: str) -> str:
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_hit_and_ttl_expiry(self):
        path = self._write("f.txt", "v1")
        first = asyncio.run(execute_tool_checked("read_file", {"file_path": path}, session_id="s1"))
        second = asyncio.run(execute_tool_checked("read_file", {"file_path": path}, session_id="s1"))
        self.assertEqual(first, second)
        self.assertTrue(self._audit_lines()[-1]["cached"])

        # 强制过期 → 重新执行
        key = list(hooks._tool_cache)[0]
        expires, result, mtime = hooks._tool_cache[key]
        hooks._tool_cache[key] = (time.monotonic() - 1, result, mtime)
        third = asyncio.run(execute_tool_checked("read_file", {"file_path": path}, session_id="s1"))
        self.assertEqual(third, first)
        self.assertFalse(self._audit_lines()[-1]["cached"])

    def test_mtime_invalidation_for_read_file(self):
        path = self._write("g.txt", "v1")
        asyncio.run(execute_tool_checked("read_file", {"file_path": path}, session_id="s1"))
        time.sleep(0.02)
        (self.tmp / "g.txt").write_text("v2", encoding="utf-8")  # 外部修改
        result = asyncio.run(execute_tool_checked("read_file", {"file_path": path}, session_id="s1"))
        self.assertIn("v2", result)

    def test_write_invalidates_whole_cache(self):
        f1 = self._write("h1.txt", "aaa")
        f2 = self._write("h2.txt", "bbb")
        asyncio.run(execute_tool_checked("read_file", {"file_path": f1}, session_id="s1"))
        asyncio.run(execute_tool_checked("read_file", {"file_path": f2}, session_id="s1"))
        self.assertEqual(len(hooks._tool_cache), 2)

        # 写 h1 → 整表清空 → h2 也要重读
        asyncio.run(execute_tool_checked(
            "write_file", {"file_path": f1, "content": "changed"}, session_id="s1"))
        self.assertEqual(len(hooks._tool_cache), 0)

        result = asyncio.run(execute_tool_checked("read_file", {"file_path": f2}, session_id="s1"))
        self.assertIn("bbb", result)
        self.assertFalse(self._audit_lines()[-1]["cached"])

    def test_errors_and_non_cacheable_not_cached(self):
        cache_put("read_file", {"file_path": "x"}, "Error: nope")
        self.assertIsNone(cache_get("read_file", {"file_path": "x"}))

        cache_put("run_shell", {"command": "dir"}, "output")
        self.assertIsNone(cache_get("run_shell", {"command": "dir"}))

        asyncio.run(execute_tool_checked("run_shell", {"command": "echo hi"}, session_id="s1"))
        self.assertFalse(self._audit_lines()[-1]["cached"])


class AuditTests(HooksTestBase):
    def test_long_input_truncated(self):
        asyncio.run(execute_tool_checked(
            "write_file", {"file_path": str(self.tmp / "t.txt"), "content": "x" * 9999}, session_id="s1"))
        record = self._audit_lines()[0]
        self.assertLess(len(json.dumps(record["input"])), 3000)
        self.assertIn("chars", str(record["input"]["content"]))

    def test_read_audit_roundtrip(self):
        asyncio.run(execute_tool_checked(
            "write_file", {"file_path": str(self.tmp / "t2.txt"), "content": "y"}, session_id="s1"))
        records = hooks.read_audit("s1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool"], "write_file")


if __name__ == "__main__":
    unittest.main()
