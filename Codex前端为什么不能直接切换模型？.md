# Codex 前端为什么不能切换其他模型？以及如何通过代理实现

## 一句话总结

Codex 是 OpenAI 的官方客户端，它只认 OpenAI 自己的 API 和模型。想用 DeepSeek、智谱 GLM、Claude 等其他模型？**需要一个代理服务器做"翻译"。**

---

## 一、背景：Codex 到底是什么？

Codex 是 OpenAI 在 2025 年底推出的桌面端 AI 编程助手（类似 Cursor、Claude Code），有 Windows 和 Mac 版本。它的核心卖点：

- **强大的工具调用能力**：可以执行 shell 命令、读写文件、搜索代码
- **沙盒环境**：在隔离环境中运行，安全可控
- **上下文管理**：自动维护项目上下文，理解代码库结构
- **流式推理**：支持 GPT-5 系列的思维链（reasoning）输出

但它的后端 API 是**完全锁死在 OpenAI 生态内的**。

---

## 二、为什么 Codex 前端不能直接切换其他模型？

### 1. 协议层面不兼容

Codex 内部使用的是 **OpenAI Responses API**，而不是通用的 Chat Completions API。这两个协议的请求/响应格式完全不同：

```
Codex 发出的请求（Responses API）：
POST /v1/responses
{
  "model": "gpt-5.4",
  "input": [
    {"type": "message", "role": "user", "content": "你好"}
  ],
  "tools": [...]
}

其他模型期望的请求（Chat Completions API）：
POST /v1/chat/completions
{
  "model": "deepseek-v4-pro",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "tools": [...]
}
```

**"input" vs "messages"，"message" vs "chat/completions"——连字段名都不一样。**

### 2. 流式响应格式不同

Codex 的 Responses API 使用自定义 SSE 事件：

```
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"你好"}

event: response.completed
data: {"type":"response.completed","response":{...}}
```

而 DeepSeek、智谱等模型返回的是标准 Chat Completions SSE：

```
data: {"choices":[{"delta":{"content":"你好"}}]}
data: [DONE]
```

**Codex 前端不识别 Chat Completions 格式的 SSE，收到会直接报错或白屏。**

### 3. reasoning_content 的特殊处理

DeepSeek V4 Pro 的思维链模式会返回 `reasoning_content` 字段。但 Codex 的 Responses API 不认识这个字段——**它会直接丢弃掉**。更致命的是，DeepSeek 要求后续请求中必须把历史的 `reasoning_content` 原样传回去，否则会报 400 错误。

这形成了一个死结：Codex 丢掉了 reasoning_content → 下次请求缺少它 → DeepSeek 报错 → 对话中断。

### 4. tool_calls 的拼接方式不同

Codex 发出的 function_call 是独立的 item：

```json
{"type": "message", "role": "assistant", "content": "好的，让我看看..."}
{"type": "function_call", "name": "shell", "arguments": "..."}
```

但 Chat Completions API 要求它们合并在同一个 message 里：

```json
{"role": "assistant", "content": "好的...", "tool_calls": [...]}
```

**格式不一致，Codex 发出的请求直接发给其他模型会报格式错误。**

---

## 三、切换其他模型有什么意义？

### 成本大幅降低

| 模型 | 价格（每百万 token） |
|------|---------------------|
| GPT-5.5（OpenAI 官方） | $15-30 |
| DeepSeek V4 Pro | ¥2-4 |
| 智谱 GLM-4-Plus | ¥5-10 |

**同样的 Codex 功能，用 DeepSeek 代理可以便宜 10-20 倍。**

### 模型能力不弱

- **DeepSeek V4 Pro**：中文能力极强，代码能力接近 GPT-5，推理能力优秀
- **智谱 GLM-4-Plus**：国内部署，延迟低，中文理解好
- **Claude**：Anthropic 的模型，长文本理解能力突出

### Codex 的核心功能不依赖模型

Codex 的工具调用（执行命令、读写文件、搜索代码）、沙盒环境、上下文管理等功能，**都是 Codex 客户端本身的能力**，不是模型的能力。只要代理正确翻译协议，这些功能完全可以正常工作。

**换句话说：你用 Codex 的界面和工具链，但底层跑的是你选的模型。**

---

## 四、代理部署实战

### 架构原理

```
Codex 桌面端 → localhost:9090（Flask 代理）→ api.deepseek.com（Chat Completions API）
```

代理的作用就是做"协议翻译"：

1. **请求翻译**：把 Codex 发出的 Responses API 请求转换成 Chat Completions 格式
2. **响应翻译**：把模型返回的 Chat Completions SSE 转换成 Codex 能识别的 Responses API SSE
3. **reasoning_content 存储**：本地维护一个 JSON 文件，保存和恢复思维链内容
4. **消息合并**：把 Codex 拆开的 assistant 消息和 function_call 合并成标准格式

### 第一步：获取 API Key

#### DeepSeek（推荐）

1. 访问 [platform.deepseek.com](https://platform.deepseek.com)
2. 注册并登录
3. 在「API Keys」页面创建一个新的 Key
4. 记下 Key（格式：`sk-xxxxxxxxxxxx`）

#### 智谱 GLM

1. 访问 [open.bigmodel.cn](https://open.bigmodel.cn)
2. 注册并登录
3. 在「API密钥」页面创建 Key
4. 记下 Key

### 第二步：下载代理代码

代理文件位于项目目录中的 `codex_proxy.py`。

核心代码结构：

```python
# 模型映射：Codex 内部用 GPT 系列名，代理自动转换为其他模型
MODEL_MAP = {
    "gpt-5.4": "deepseek-v4-pro",
    "gpt-5.4-mini": "deepseek-v4-flash",
    "gpt-4o": "deepseek-v4-pro",
    "gpt-4o-mini": "deepseek-v4-flash",
}

# 代理地址
UPSTREAM_BASE = "https://api.deepseek.com"
```

### 第三步：配置 Codex

编辑 `C:\Users\你的用户名\.codex\config.toml`：

```toml
model = "gpt-5.4"
model_provider = "deepseek"
sandbox_mode = "danger-full-access"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://localhost:9090/v1"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
```

**关键配置说明：**

- `model = "gpt-5.4"`：Codex 内部使用的模型名（会被代理映射为 DeepSeek 模型）
- `model_provider = "deepseek"`：指定使用自定义 provider
- `base_url = "http://localhost:9090/v1"`：指向本地代理
- `wire_api = "responses"`：告诉 Codex 使用 Responses API 格式

### 第四步：设置环境变量

在系统环境变量中添加：

```
DEEPSEEK_API_KEY = sk-xxxxxxxxxxxx
```

或者在 BAT 启动脚本中设置：

```bat
@echo off
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx
python codex_proxy.py --upstream https://api.deepseek.com --port 9090
```

### 第五步：启动代理

1. 双击桌面上的 `启动Codex代理.bat`
2. 等待看到 `Running on http://127.0.0.1:9090`
3. 代理启动成功

### 第六步：打开 Codex

1. 打开 Codex 桌面端
2. 正常对话即可

**注意启动顺序：必须先启动代理，再打开 Codex。** Codex 启动时就会连接代理，代理没跑会导致白屏。

---

## 五、常见问题

### Q1：用 DeepSeek 代理后，Codex 的功能还能用吗？

**能用。** Codex 的核心功能（执行命令、读写文件、搜索代码、沙盒环境）都是客户端的能力，与底层模型无关。代理只负责翻译协议，不影响功能。

### Q2：GPT-5.5 的 Codex 功能会更强吗？

Codex 的功能强弱取决于客户端本身，不是模型。GPT-5.5 作为模型可能推理能力更强，但 Codex 的工具调用、沙盒等能力不会因为换了模型而丢失。

### Q3：代理会不会很慢？

代理本身只是做协议转换，延迟增加约 50-100ms，几乎无感。主要延迟来自模型本身的推理速度。DeepSeek V4 Pro 的速度很快，体验流畅。

### Q4：白屏了怎么办？

清空 `~/.codex/.codex-global-state.json` 中的 `active-workspace-roots` 和 `projectless-thread-ids`，然后重启 Codex。

### Q5：多进程冲突怎么解决？

Windows 上多个 Python 进程可以同时绑定同一端口。每次重启代理前，先杀掉所有旧的 Python 进程：

```bash
taskkill //F //IM python.exe
```

### Q6：可以切换其他模型吗？

可以。只需修改代理中的 `MODEL_MAP` 和 `UPSTREAM_BASE`：

```python
# 智谱 GLM
MODEL_MAP = {
    "gpt-5.4": "glm-4-plus",
    "gpt-5.4-mini": "glm-4-flash",
}
UPSTREAM_BASE = "https://open.bigmodel.cn/api/paas/v4"
```

### Q7：更新 Codex 后配置会丢失吗？

会。每次更新 Codex 前，建议备份 `config.toml`。更新后检查模型名是否变化（比如 `gpt-5.4` 变成 `gpt-5.5`），如有变化需同步更新 `MODEL_MAP`。

---

## 六、总结

| 项目 | 说明 |
|------|------|
| 为什么不能直接切换 | 协议不兼容（Responses API vs Chat Completions API） |
| 切换的意义 | 成本降低 10-20 倍，模型能力不弱 |
| 核心功能会丢失吗 | 不会，功能在客户端，不在模型 |
| 部署难度 | 中等，需要一个 Flask 代理做协议翻译 |
| 支持的模型 | DeepSeek、智谱 GLM、以及其他 OpenAI 兼容 API |

**Codex 是一个优秀的前端工具，代理让它摆脱了对 OpenAI 模型的绑定，以更低的成本享受同样的功能。**
