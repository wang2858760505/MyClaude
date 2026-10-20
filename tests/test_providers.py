"""providers.py 注册表解析逻辑的单元测试。

覆盖：自动探测顺序、显式指定、环境变量指定、端点覆盖优先级、错误路径。
全部通过 monkeypatch 操作环境变量，不依赖机器上的真实 key。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MyClaude.providers import REGISTRY, ProviderError, resolve_provider

ALL_KEY_VARS = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
ALL_URL_VARS = ("DEEPSEEK_BASE_URL", "DASHSCOPE_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "MYCLAUDE_PROVIDER")


class ProviderResolutionTests(unittest.TestCase):
    def setUp(self):
        """每个用例都在干净的环境变量下运行。"""
        for var in ALL_KEY_VARS + ALL_URL_VARS:
            os.environ.pop(var, None)

    def tearDown(self):
        for var in ALL_KEY_VARS + ALL_URL_VARS:
            os.environ.pop(var, None)

    # ── 自动探测 ──

    def test_search_order_prefers_deepseek(self):
        """SEARCH_ORDER 中排前的供应商胜出：deepseek > qwen > anthropic > openai。"""
        os.environ["DASHSCOPE_API_KEY"] = "sk-q"
        os.environ["DEEPSEEK_API_KEY"] = "sk-d"
        r = resolve_provider()
        self.assertEqual(r.provider.name, "deepseek")
        self.assertEqual(r.base_url, "https://api.deepseek.com/v1")
        self.assertTrue(r.use_openai)

    def test_search_order_falls_to_qwen(self):
        os.environ["DASHSCOPE_API_KEY"] = "sk-q"
        r = resolve_provider()
        self.assertEqual(r.provider.name, "qwen")
        self.assertEqual(r.provider.default_model, "qwen-plus")
        self.assertEqual(r.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")

    # ── 显式指定 ──

    def test_explicit_provider_beats_search_order(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-d"
        os.environ["DASHSCOPE_API_KEY"] = "sk-q"
        r = resolve_provider("qwen")
        self.assertEqual(r.provider.name, "qwen")
        self.assertEqual(r.api_key, "sk-q")

    def test_myclaude_provider_env(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-d"
        os.environ["DASHSCOPE_API_KEY"] = "sk-q"
        os.environ["MYCLAUDE_PROVIDER"] = "qwen"
        r = resolve_provider()
        self.assertEqual(r.provider.name, "qwen")

    def test_explicit_provider_without_key_raises(self):
        """显式指定了 key 未配置的供应商 → 明确报错。"""
        with self.assertRaises(ProviderError):
            resolve_provider("anthropic")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ProviderError):
            resolve_provider("gpt9")

    def test_unknown_myclaude_provider_env_raises(self):
        os.environ["MYCLAUDE_PROVIDER"] = "nope"
        with self.assertRaises(ProviderError):
            resolve_provider()

    # ── 无 key 的错误路径 ──

    def test_no_keys_raises_with_guidance(self):
        with self.assertRaises(ProviderError) as ctx:
            resolve_provider()
        msg = str(ctx.exception)
        for var in ALL_KEY_VARS:
            self.assertIn(var, msg)  # 报错要列出每个缺失的 key 变量名

    # ── 端点解析优先级 ──

    def test_api_base_override_wins(self):
        """--api-base 参数优先级最高，覆盖供应商默认端点。"""
        os.environ["DEEPSEEK_API_KEY"] = "sk-d"
        r = resolve_provider("deepseek", "https://proxy.example/v1")
        self.assertEqual(r.base_url, "https://proxy.example/v1")

    def test_base_url_env_fills_openai_default(self):
        """openai 供应商无内置端点，由 OPENAI_BASE_URL 环境变量补齐。"""
        os.environ["OPENAI_API_KEY"] = "sk-o"
        os.environ["OPENAI_BASE_URL"] = "https://aihubmix.com/v1"
        r = resolve_provider("openai")
        self.assertEqual(r.base_url, "https://aihubmix.com/v1")
        self.assertTrue(r.use_openai)

    def test_anthropic_protocol_detection(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-a"
        r = resolve_provider("anthropic")
        self.assertFalse(r.use_openai)

    # ── 注册表数据完整性 ──

    def test_registry_covers_search_order(self):
        """SEARCH_ORDER 里的每个名字都必须在 REGISTRY 中（防拼错）。"""
        from MyClaude.providers import SEARCH_ORDER
        for name in SEARCH_ORDER:
            self.assertIn(name, REGISTRY)


if __name__ == "__main__":
    unittest.main()
