"""check_provider_health 的单元测试（mock httpx，不发真实网络请求）。

覆盖：200 健康、401 key 无效、404 网关兜底健康、连接失败、超时、
协议头差异（anthropic x-api-key vs openai Bearer）、默认端点兜底。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import httpx  # 旧依赖链（SDK 1.x 时代）的传递依赖
except ImportError:  # 新版 SDK 依赖链只装 httpx2，API 兼容
    import httpx2 as httpx

from MyClaude.providers import DEFAULT_ENDPOINTS, ResolvedProvider, Provider, check_provider_health


def make_resolved(protocol: str = "openai", base_url: str | None = None) -> ResolvedProvider:
    return ResolvedProvider(
        provider=Provider(name="test", api_key_env="TEST_KEY", default_model="m", protocol=protocol),
        api_key="sk-test",
        base_url=base_url,
        protocol=protocol,
        use_openai=protocol in ("openai", "responses"),
    )


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """替换 httpx.AsyncClient：记录请求参数并回放预设响应或异常。"""

    def __init__(self, behavior, **kw):
        self.behavior = behavior
        self.kw = kw
        _FakeClient.last_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        _FakeClient.last_url = url
        _FakeClient.last_headers = headers or {}
        b = self.behavior
        if isinstance(b, Exception):
            raise b
        return _FakeResponse(b)


def run_check(resolved, behavior) -> dict:
    with mock.patch.object(httpx, "AsyncClient",
                           lambda **kw: _FakeClient(behavior, **kw)):
        return asyncio.run(check_provider_health(resolved))


class HealthCheckTests(unittest.TestCase):
    def test_200_is_healthy(self):
        r = run_check(make_resolved(), 200)
        self.assertTrue(r["ok"])
        self.assertIn("API key valid", r["detail"])
        self.assertGreaterEqual(r["latency_ms"], 0)

    def test_401_key_rejected(self):
        r = run_check(make_resolved(), 401)
        self.assertFalse(r["ok"])
        self.assertIn("API key rejected", r["detail"])
        self.assertIn("TEST_KEY", r["detail"])

    def test_403_key_rejected(self):
        self.assertFalse(run_check(make_resolved(), 403)["ok"])

    def test_404_gateway_fallback_healthy(self):
        """第三方网关常不暴露 /models —— 端点可达即视为健康。"""
        r = run_check(make_resolved(), 404)
        self.assertTrue(r["ok"])
        self.assertIn("404", r["detail"])

    def test_500_unhealthy(self):
        self.assertFalse(run_check(make_resolved(), 500)["ok"])

    def test_connection_error(self):
        r = run_check(make_resolved(), httpx.ConnectError("refused"))
        self.assertFalse(r["ok"])
        self.assertIn("Connection failed", r["detail"])

    def test_timeout(self):
        r = run_check(make_resolved(), httpx.ConnectTimeout("slow"))
        self.assertFalse(r["ok"])
        self.assertIn("Timeout", r["detail"])

    # ── 协议头与端点 ──

    def test_openai_bearer_header_and_default_endpoint(self):
        run_check(make_resolved("openai", base_url=None), 200)
        self.assertEqual(_FakeClient.last_url, f"{DEFAULT_ENDPOINTS['openai']}/models")
        self.assertEqual(_FakeClient.last_headers["Authorization"], "Bearer sk-test")
        self.assertNotIn("x-api-key", _FakeClient.last_headers)

    def test_anthropic_headers_and_default_endpoint(self):
        run_check(make_resolved("anthropic", base_url=None), 200)
        self.assertEqual(_FakeClient.last_url, f"{DEFAULT_ENDPOINTS['anthropic']}/models")
        self.assertEqual(_FakeClient.last_headers["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", _FakeClient.last_headers)

    def test_base_url_override_wins(self):
        run_check(make_resolved("openai", base_url="https://proxy.example/v1"), 200)
        self.assertEqual(_FakeClient.last_url, "https://proxy.example/v1/models")

    def test_responses_protocol_shares_openai_endpoint(self):
        run_check(make_resolved("responses", base_url=None), 200)
        self.assertEqual(_FakeClient.last_url, f"{DEFAULT_ENDPOINTS['responses']}/models")


if __name__ == "__main__":
    unittest.main()
