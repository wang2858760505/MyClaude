"""_slugify 的单元测试：中文标题必须产生非空的 slug。

回归用例：旧实现只保留 [a-z0-9]，中文标题会被压成空串，
save_memory 便把所有中文记忆写到同一个 `{type}_.md`，互相覆盖。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude.memory import _slugify


class TestSlugifyCJK(unittest.TestCase):
    def test_chinese_title_keeps_characters(self):
        self.assertEqual(_slugify("项目笔记"), "项目笔记")

    def test_distinct_chinese_titles_do_not_collide(self):
        self.assertNotEqual(_slugify("项目笔记"), _slugify("周报记录"))

    def test_ascii_title_unchanged(self):
        self.assertEqual(_slugify("Deploy Flow"), "deploy_flow")


if __name__ == "__main__":
    unittest.main()
