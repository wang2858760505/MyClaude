"""LLM 供应商注册表 — 声明式配置，新增供应商只需在 REGISTRY 加一个条目。

设计模式：注册表（Registry）+ 数据驱动。

解析逻辑只有一个通用函数 resolve_provider()：
  1. 每家供应商描述为一条纯数据（Provider），集中在 REGISTRY
  2. 用户的显式选择（--provider / MYCLAUDE_PROVIDER）优先于自动探测
  3. 自动探测时按 SEARCH_ORDER 遍历，第一个 key 就位的供应商胜出

这样"加供应商"只是加一条数据，解析函数是纯函数（只读环境变量、
不改任何状态），可以低成本写单元测试。

协议说明（三种 protocol）：
  - "anthropic"  ：Anthropic Messages API（claude 系列）
  - "openai"     ：OpenAI Chat Completions 兼容格式 —— 已经是行业事实标准，
                   DeepSeek / Qwen(DashScope) / Kimi / GLM / OpenRouter 等都兼容它，
                   所以它们全部复用 openai SDK，只是 base_url 和模型名不同。
  - "responses"  ：OpenAI Responses API —— Chat Completions 之后的新一代有状态
                   接口（instructions / input items / 原生 function_call 流事件）。
                   它与 "openai" 共用同一套 openai SDK 和本地规范化消息历史，
                   仅在请求层做格式适配（见 core._call_responses_stream），
                   因此 agent 层的 use_openai 对两种协议都为 True。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """一家供应商的静态声明（不含任何运行时状态）。"""

    name: str                    # 供应商标识，用于 --provider 和报错提示
    api_key_env: str             # 从哪个环境变量读 API key
    default_model: str           # 用户没指定模型时的兜底
    protocol: str                # "openai" | "anthropic" | "responses"，决定走哪套调用路径
    base_url: str | None = None      # API 端点；None = 用该协议 SDK 的官方默认
    base_url_env: str | None = None  # 允许环境变量覆盖端点（第三方代理场景）


# ── 注册表 ────────────────────────────────────────────────────────────
# 新增供应商 = 在这里加一条，其他代码零改动。
REGISTRY: dict[str, Provider] = {
    # 专用 key 优先探测：设了 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 的意图非常明确，
    # 应当优先于泛用的 OPENAI_API_KEY（后者常被其他工具占用）。
    "deepseek": Provider(
        name="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",   # DeepSeek API 官方报告的当前模型名
        protocol="openai",
        base_url="https://api.deepseek.com/v1",
        base_url_env="DEEPSEEK_BASE_URL",
    ),
    "qwen": Provider(
        name="qwen",
        api_key_env="DASHSCOPE_API_KEY",     # 阿里云百炼的 key 惯用名
        default_model="qwen-plus",           # 官方稳定别名，效果/成本均衡
        protocol="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        base_url_env="DASHSCOPE_BASE_URL",
    ),
    "anthropic": Provider(
        name="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-opus-4-6",
        protocol="anthropic",
        base_url=None,                       # anthropic SDK 默认官方端点
        base_url_env="ANTHROPIC_BASE_URL",
    ),
    "openai": Provider(
        name="openai",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o",
        protocol="openai",
        base_url=None,                       # openai SDK 默认官方端点
        base_url_env="OPENAI_BASE_URL",
    ),
    # Responses API 是显式选择项（不进 SEARCH_ORDER）：它和 openai 共用
    # OPENAI_API_KEY，自动探测永远轮不到它，只有 --provider openai-responses
    # 或 MYCLAUDE_PROVIDER=openai-responses 才会启用。
    "openai-responses": Provider(
        name="openai-responses",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o",
        protocol="responses",
        base_url=None,
        base_url_env="OPENAI_BASE_URL",
    ),
}

# 自动探测顺序：--provider > MYCLAUDE_PROVIDER 环境变量 > 此列表从上到下
SEARCH_ORDER: list[str] = ["deepseek", "qwen", "anthropic", "openai"]


class ProviderError(Exception):
    """没有任何供应商的 API key 就位时抛出，message 直接面向用户展示。"""


@dataclass(frozen=True)
class ResolvedProvider:
    """解析结果：选中的供应商 + 运行时凭证（key 实际值、实际端点）。"""

    provider: Provider
    api_key: str
    base_url: str | None      # 已合并 默认端点 + 环境变量覆盖 + --api-base 参数
    protocol: str             # "openai" | "anthropic" | "responses"
    use_openai: bool          # True → openai 协议族（Chat Completions 或 Responses）；
                              # False → anthropic SDK。openai 族共享同一套本地消息历史，
                              # 仅线上请求格式不同（见 core._call_responses_stream）


def resolve_provider(explicit: str | None = None, api_base_override: str | None = None) -> ResolvedProvider:
    """选出可用的供应商并解析出最终连接配置。

    优先级：explicit（--provider）> MYCLAUDE_PROVIDER 环境变量 > SEARCH_ORDER 自动探测。
    api_base_override 来自 --api-base 参数，优先级高于一切端点配置。

    Raises:
        ProviderError: 指定的供应商不存在，或所有候选供应商都没有配置 API key。
    """
    # ① 确定候选顺序
    if explicit:
        chosen = explicit.strip().lower()
        if chosen not in REGISTRY:
            raise ProviderError(
                f"Unknown provider: '{explicit}'.\n"
                f"  Available: {', '.join(REGISTRY)}"
            )
        candidates = [chosen]
    else:
        env_choice = os.environ.get("MYCLAUDE_PROVIDER")
        if env_choice:
            chosen = env_choice.strip().lower()
            if chosen not in REGISTRY:
                raise ProviderError(f"Unknown MYCLAUDE_PROVIDER: '{env_choice}'")
            candidates = [chosen]
        else:
            candidates = SEARCH_ORDER

    # ② 找到第一个 key 就位的供应商
    missing: list[str] = []
    for name in candidates:
        p = REGISTRY[name]
        api_key = os.environ.get(p.api_key_env)
        if not api_key:
            missing.append(f"{p.api_key_env} (for {name})")
            continue

        # ③ 解析最终端点：--api-base 参数 > 环境变量覆盖 > 供应商默认
        base_url = p.base_url
        if p.base_url_env:
            base_url = base_url or os.environ.get(p.base_url_env)
        if api_base_override:
            base_url = api_base_override

        return ResolvedProvider(
            provider=p,
            api_key=api_key,
            base_url=base_url,
            protocol=p.protocol,
            # "responses" 归入 openai 协议族：共享 SDK 客户端、本地消息历史、
            # 压缩/会话/子代理等全部 openai 分支逻辑，只有请求层格式不同。
            use_openai=p.protocol in ("openai", "responses"),
        )

    raise ProviderError(
        "No API key found for any provider.\n"
        "  Set one of the following environment variables:\n    " + "\n    ".join(missing)
    )


# ── 运行时健康检查（IO，非纯函数；供 --check 启动预检使用）──────────────

# 协议默认官方端点（供应商 base_url 为 None 时兜底）
DEFAULT_ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "responses": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


async def check_provider_health(resolved: ResolvedProvider, timeout: float = 8.0) -> dict:
    """对选中供应商发一次 GET /models 轻量探测，验证两件事：
    端点可达 + API key 有效。不消耗模型 token（models 列表不计费）。

    返回 {"ok": bool, "latency_ms": float, "detail": str}。
    网关 404 视为基本健康（部分第三方网关不暴露 /models，但 chat 端点可用）。
    """
    try:
        import httpx  # 旧依赖链（SDK 1.x 时代）的传递依赖
    except ImportError:  # 新版 SDK 依赖链只装 httpx2，API 兼容
        import httpx2 as httpx

    proto = resolved.provider.protocol
    base = (resolved.base_url or DEFAULT_ENDPOINTS.get(proto, "")).rstrip("/")
    url = f"{base}/models"
    if proto == "anthropic":
        headers = {"x-api-key": resolved.api_key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {resolved.api_key}"}

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "latency_ms": (time.monotonic() - start) * 1000,
                "detail": f"Timeout connecting to {url}"}
    except httpx.HTTPError as e:
        return {"ok": False, "latency_ms": (time.monotonic() - start) * 1000,
                "detail": f"Connection failed: {e}"}

    latency_ms = (time.monotonic() - start) * 1000
    if resp.status_code == 200:
        return {"ok": True, "latency_ms": latency_ms, "detail": f"API key valid, endpoint {url} reachable"}
    if resp.status_code in (401, 403):
        return {"ok": False, "latency_ms": latency_ms,
                "detail": f"HTTP {resp.status_code}: API key rejected ({resolved.provider.api_key_env})"}
    if resp.status_code == 404:
        return {"ok": True, "latency_ms": latency_ms,
                "detail": f"HTTP 404 on /models (gateway may not expose it) — endpoint reachable"}
    return {"ok": False, "latency_ms": latency_ms, "detail": f"HTTP {resp.status_code} from {url}"}
