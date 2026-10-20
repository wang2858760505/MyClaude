# MyClaude

> 一个纯 Python 实现的本地化编程 Agent（类 Claude Code）。
>
> 把你的意图交给 LLM，Agent 在本地安全地读文件、写代码、执行命令，多轮自主推进直到完成任务。

---

## ✨ 特性

| 类别 | 功能 |
|------|------|
| 🧠 **Agent 核心** | 多轮工具调用循环 · 流式输出 · 双协议后端（Anthropic + OpenAI 兼容） |
| 🔌 **多模型接入** | 4 家供应商开箱即用（DeepSeek / Qwen / Anthropic / OpenAI），声明式注册表扩展 |
| 🛡️ **权限安全** | 5 级裁决链（deny 规则 → plan 模式 → allow 规则 → 只读白名单 → 危险确认）· fail-closed |
| 🗜️ **长任务支撑** | 4 层上下文压缩管线（预算裁剪 → 旧结果剪除 → 空闲微压缩 → LLM 摘要）· prompt cache 感知 |
| 🤖 **自主模式** | `/goal` LLM 评估循环 · `/loop` 定时/动态唤醒 · Auto Mode LLM 分类器替代人工确认 |
| 🧩 **扩展能力** | MCP 协议（JSON-RPC over stdio）外部工具 · Skill 提示词模板 · 子代理并行分包 |
| 💾 **可恢复** | 会话 JSON 持久化 · `--resume` 断点续跑 · 跨会话记忆 |

---

## 🚀 快速开始

```bash
# 安装（在项目根目录）
pip install -e .

# 运行
myclaude                      # 交互式 REPL
myclaude "hello"              # 一次性模式
myclaude --plan "重构建议"     # 计划模式（只读，不做任何变更）
myclaude --yolo "跑测试"       # 跳过所有确认

# 不装也行：项目根目录下用模块方式运行
python -m MyClaude
```

### 配置 API Key（三选一）

MyClaude 采用**供应商自动探测**：按下方环境变量的优先级顺序，哪个有值就用哪家。**用户只需提供一个 API key 即可**，不需要额外配置"分类器模型/摘要模型"——所有辅助调用都复用同一个主模型。

```powershell
# Windows PowerShell（临时，当前窗口有效）
$env:DEEPSEEK_API_KEY = "sk-..."     # DeepSeek：platform.deepseek.com
# 或
$env:DASHSCOPE_API_KEY = "sk-..."    # Qwen（百炼）：bailian.console.aliyun.com
# 或
$env:ANTHROPIC_API_KEY = "sk-ant-..." # Anthropic：console.anthropic.com
```

**永久生效**（Windows）：把环境变量写进"用户变量"（系统属性 → 环境变量），重启终端后自动加载。

---

## 🧩 模型 / 供应商配置

| 供应商 | API Key 环境变量 | 默认模型 | 协议 |
|--------|------------------|----------|------|
| **DeepSeek** | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` | OpenAI 兼容 |
| **Qwen**（阿里云百炼） | `DASHSCOPE_API_KEY` | `qwen-plus` | OpenAI 兼容 |
| **Anthropic** | `ANTHROPIC_API_KEY` | `claude-opus-4-6` | Anthropic Messages |
| **OpenAI 兼容**（任意代理） | `OPENAI_API_KEY` + `OPENAI_BASE_URL` | `gpt-4o` | OpenAI 兼容 |

### 常用命令

```bash
# 显式指定供应商 / 模型
myclaude -p qwen -m qwen3.8-max            "分析这段代码"
myclaude -p deepseek -m deepseek-v4-pro    "做一个代码评审"

# 设置默认模型（优先级：--model > MYCLAUDE_MODEL > 供应商默认）
$env:MYCLAUDE_MODEL = "deepseek-v4-flash"   # PowerShell
export MYCLAUDE_MODEL="deepseek-v4-flash"   # bash

# 第三方代理 / 自建兼容端点
myclaude --api-base https://your-proxy/v1 -m your-model "hello"
```

> 💡 **扩展供应商**：注册表位于 `MyClaude/providers.py`，新增一家供应商只需在 `REGISTRY` 中加一条 Provider 条目（约 6 行纯数据），无需改动任何解析逻辑。

---

## 🧰 REPL 命令

进入 REPL（`myclaude` 不带参数）后，除了正常对话之外：

| 命令 | 作用 |
|------|------|
| `/clear` | 清空对话历史 |
| `/plan` | 切换计划模式（只读 ↔ 正常） |
| `/cost` | 显示 token 用量与累计成本 |
| `/compact` | 手动压缩当前上下文 |
| `/goal <条件>` | 设定目标，Agent 自主循环直到 LLM 评估器判定达成 |
| `/goal` | 查看当前目标状态 |
| `/loop [间隔] <prompt>` | 定时循环（`5m`/`2h`）或动态自调度 |
| `/memory` | 列出跨会话保存的记忆 |
| `/skills` | 列出可用技能 |
| `/<skill名> [参数]` | 调用技能 |
| `exit` | 退出 |

### 权限模式（启动参数）

| 模式 | 行为 |
|------|------|
| 默认 | 危险操作逐项确认 |
| `--yolo` | 跳过所有确认（全放行，本地信任场景使用） |
| `--plan` | 只读：除计划文件外不写任何东西、不跑任何命令 |
| `--accept-edits` | 自动批准文件编辑，危险 shell 仍确认 |
| `--dont-ask` | 自动拒绝所有需要确认的操作（CI 安全模式） |
| `--auto` | LLM 分类器替代人工：允许安全操作，拦截高风险操作并给出理由 |

---

## 🏗️ 架构

```
┌──────────────────────────────────────────────┐
│ __main__.py        CLI 参数解析 + REPL 循环    │
├──────────────────────────────────────────────┤
│ agent/ 子包（Mixin 组合式架构）                │
│   core.py          对话循环 + 工具分发          │
│   compression.py   4 层上下文压缩管线           │
│   autonomy_runner  /goal /loop 执行循环        │
│   auto_permission  Auto Mode 分类器            │
│   budget.py        成本统计 + 预算控制         │
│   special_tools    plan/skill/子代理执行体      │
│   session_store    会话持久化                  │
│   constants.py     共享常量与导入              │
├──────────────────────────────────────────────┤
│ providers.py       供应商注册表 + 解析（纯数据） │
│ tools.py           12+ 工具 + 5 级权限裁决      │
│ prompt.py          系统提示词组装               │
│ mcp_client.py      MCP 外部工具客户端           │
├──────────────────────────────────────────────┤
│ session  memory  skills  subagent              │
│ autonomy  frontmatter  ui                      │
│  （职责单一、无内部依赖的纯模块）                │
└──────────────────────────────────────────────┘
```

### 一次对话的生命周期

1. **入口**：REPL 收到输入 → 调用 `agent.chat(inp)`
2. **装配**：提示词模板 + 工作区文件树 + git 状态 + CLAUDE.md → 构造系统提示
3. **请求**：历史 + 工具 JSON Schema → 发给 LLM（流式）
4. **裁决**：LLM 要调用工具时，先过 `check_permission`（5 级权限链）
5. **执行**：本地执行工具（读/写/命令/Web/子代理/MCP…）
6. **回填**：执行结果追加进历史 → 回到第 3 步再次请求
7. **终止**：LLM 回复纯文本 → 展示给用户 → 会话 JSON 落盘

所有"辅助调用"（Auto Mode 分类器、目标评估器、记忆预取、上下文摘要）全部**复用同一个主模型和同一个 API key**，仅按需缩小 `max_tokens` 或设置 `temperature=0`，无需用户提供独立的"小模型"配置。

### 测试

```bash
python -m pytest tests/ -q
# 56 个单元测试：供应商注册表 · 权限裁决 · 4 层压缩管线 · Auto Mode 分类器契约
```

---

## 📚 开发哲学

- **Fail-closed**：任何模糊情况（权限规则不匹配、LLM 输出解析失败）一律倒向"拒绝"
- **缓存优先**：压缩管线对 Prompt Cache 的破坏设门控，宁晚压缩也不盲目失效缓存
- **纯函数隔离**：可测逻辑（分类器转录生成、评估器解析、规则加载）不做 IO
- **数据驱动扩展**：新增供应商/工具的改动限定在配置数据，不改核心路径

---

## 🛤️ 路线图

- [ ] Backend 抽象：统一双后端 7 对方法的消息转换逻辑
- [ ] 工具装饰器注册：新增工具从 3 处改 1 处
- [ ] 辅助模型独立配置：允许为分类器/摘要指定成本更低的小模型
- [ ] Web UI：流式对话 + 工具执行可视化

---

## 📄 License

MIT
