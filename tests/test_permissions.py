"""check_permission 权限检查的单元测试。

check_permission 的判定顺序（也是回归时要保护的行为契约）：
  1. deny 规则      —— 最高优先级，连 --yolo 都拦
  2. plan 模式      —— 只读契约，除 plan 文件外一切写操作/shell 拒绝
  3. bypassPermissions —— 全放行
  4. allow 规则
  5. 只读工具白名单
  6. acceptEdits 对编辑工具放行
  7. 危险操作 → confirm（dontAsk 模式下 → deny）
  8. 其余放行

规则加载依赖文件系统 + 全局缓存，测试直接注入缓存 dict 绕过 IO。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude import tools


def set_rules(allow: list[str] | None = None, deny: list[str] | None = None):
    """把权限规则缓存替换为测试规则，绕过文件加载。"""
    tools._cached_rules = {
        "allow": [tools._parse_rule(r) for r in (allow or [])],
        "deny": [tools._parse_rule(r) for r in (deny or [])],
    }


def clear_rules():
    tools._cached_rules = None


class PermissionRuleTests(unittest.TestCase):
    def tearDown(self):
        clear_rules()

    def test_deny_rule_beats_everything(self):
        """deny 规则优先级最高：连 bypassPermissions 都被拦。"""
        set_rules(deny=["run_shell(rm *)"])
        r = tools.check_permission("run_shell", {"command": "rm -rf x"}, "bypassPermissions")
        self.assertEqual(r["action"], "deny")

    def test_deny_rule_pattern_matching(self):
        """前缀通配 * 只匹配同前缀命令。"""
        set_rules(deny=["run_shell(git push*)"])
        blocked = tools.check_permission("run_shell", {"command": "git push origin main"}, "bypassPermissions")
        allowed = tools.check_permission("run_shell", {"command": "git status"}, "default")
        self.assertEqual(blocked["action"], "deny")
        self.assertEqual(allowed["action"], "allow")

    def test_deny_rule_exact_match(self):
        set_rules(deny=["run_shell(make deploy)"])
        r = tools.check_permission("run_shell", {"command": "make deploy"}, "default")
        self.assertEqual(r["action"], "deny")
        other = tools.check_permission("run_shell", {"command": "make deploy check"}, "default")
        self.assertEqual(other["action"], "allow")  # 精确匹配不等于前缀匹配

    def test_allow_rule_in_default_mode(self):
        set_rules(allow=["write_file(src/*)"])
        r = tools.check_permission("write_file", {"file_path": "src/app.py", "content": "x"}, "default")
        self.assertEqual(r["action"], "allow")

    def test_allow_rule_other_tool_no_match(self):
        set_rules(allow=["write_file(src/*)"])
        # 不匹配 allow 规则 → 走默认逻辑：edit 已存在的文件 → allow
        existing = str(Path(__file__))
        r = tools.check_permission("edit_file", {"file_path": existing, "old_string": "a", "new_string": "b"}, "default")
        self.assertEqual(r["action"], "allow")
        # edit 不存在的文件 → confirm（默认逻辑的另一分支）
        r2 = tools.check_permission("edit_file", {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}, "default")
        self.assertEqual(r2["action"], "confirm")


class PlanModeTests(unittest.TestCase):
    def tearDown(self):
        clear_rules()

    def test_plan_mode_blocks_edit_but_allows_plan_file(self):
        set_rules()
        r = tools.check_permission("write_file", {"file_path": "other.py", "content": "x"}, "plan")
        self.assertEqual(r["action"], "deny")
        ok = tools.check_permission("write_file", {"file_path": "/plans/p.md", "content": "x"}, "plan", plan_file_path="/plans/p.md")
        self.assertEqual(ok["action"], "allow")

    def test_plan_mode_blocks_shell_even_safe(self):
        set_rules()
        r = tools.check_permission("run_shell", {"command": "echo hi"}, "plan")
        self.assertEqual(r["action"], "deny")

    def test_plan_mode_allows_reads(self):
        set_rules()
        r = tools.check_permission("read_file", {"file_path": "a.py"}, "plan")
        self.assertEqual(r["action"], "allow")

    def test_plan_mode_beats_allow_rule(self):
        """plan 模式的只读契约压过 allow 规则。"""
        set_rules(allow=["write_file(*)"])
        r = tools.check_permission("write_file", {"file_path": "x.py", "content": "y"}, "plan")
        self.assertEqual(r["action"], "deny")


class ModeTests(unittest.TestCase):
    def tearDown(self):
        clear_rules()

    def test_bypass_allows_dangerous(self):
        clear_rules()
        r = tools.check_permission("run_shell", {"command": "rm -rf build"}, "bypassPermissions")
        self.assertEqual(r["action"], "allow")

    def test_read_tools_always_allowed(self):
        clear_rules()
        for tool, inp in [
            ("read_file", {"file_path": "a.py"}),
            ("list_files", {"path": "."}),
            ("grep_search", {"pattern": "x"}),
            ("web_fetch", {"url": "https://example.com"}),
        ]:
            r = tools.check_permission(tool, inp, "default")
            self.assertEqual(r["action"], "allow", f"{tool} should be allowed")

    def test_accept_edits_allows_existing_file_edit(self):
        clear_rules()
        existing = str(Path(__file__))  # 一定存在的文件
        r = tools.check_permission("edit_file", {"file_path": existing, "old_string": "a", "new_string": "b"}, "acceptEdits")
        self.assertEqual(r["action"], "allow")

    def test_accept_edits_does_not_allow_shell(self):
        clear_rules()
        r = tools.check_permission("run_shell", {"command": "rm -rf build"}, "acceptEdits")
        self.assertEqual(r["action"], "confirm")  # 危险 shell 仍要确认

    def test_dangerous_shell_confirms_in_default(self):
        clear_rules()
        r = tools.check_permission("run_shell", {"command": "sudo apt install x"}, "default")
        self.assertEqual(r["action"], "confirm")
        self.assertEqual(r["message"], "sudo apt install x")

    def test_safe_shell_allows_in_default(self):
        clear_rules()
        r = tools.check_permission("run_shell", {"command": "echo hi"}, "default")
        self.assertEqual(r["action"], "allow")

    def test_dont_ask_denies_dangerous(self):
        clear_rules()
        r = tools.check_permission("run_shell", {"command": "rm -rf build"}, "dontAsk")
        self.assertEqual(r["action"], "deny")

    def test_write_new_file_confirms_in_default(self):
        clear_rules()
        r = tools.check_permission("write_file", {"file_path": "definitely/not/exist.py", "content": "x"}, "default")
        self.assertEqual(r["action"], "confirm")

    def test_write_existing_file_allows_in_default(self):
        clear_rules()
        r = tools.check_permission("write_file", {"file_path": str(Path(__file__)), "content": "x"}, "default")
        self.assertEqual(r["action"], "allow")


if __name__ == "__main__":
    unittest.main()
