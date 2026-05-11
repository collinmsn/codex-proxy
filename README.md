# Codex Proxy

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/Status-Stable-brightgreen" alt="Status">
  <img src="https://img.shields.io/github/stars/Lucasmantou/codex-proxy?style=social" alt="Stars">
</p>

<p align="center">
  <b>让 Codex 桌面端使用任意 LLM - 成本降低 30-50 倍</b>
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> •
  <a href="#-支持的模型">支持的模型</a> •
  <a href="#-常见问题">常见问题</a> •
  <a href="#-贡献">贡献</a> •
  <a href="#-许可证">许可证</a>
</p>

---

## ✨ 项目亮点

- **🚀 一行命令启动** - 无需复杂配置，开箱即用
- **💰 成本降低 30-50 倍** - 使用 DeepSeek 替代 GPT-5.5
- **🔌 多模型支持** - DeepSeek、智谱 GLM，以及任何 OpenAI 兼容 API
- **🧠 思维链完整** - 完美支持 DeepSeek V4 Pro 的 reasoning_content
- **🛡️ 功能完整** - Codex 的工具调用、沙盒环境、上下文管理全部正常
- **📖 开源免费** - MIT 许可证，欢迎贡献

---

## 📊 成本对比

| 模型 | 价格（每百万 token） | 相比官方 |
|------|---------------------|---------|
| GPT-5.5（OpenAI 官方） | $15-30 | 基准 |
| **DeepSeek V4 Pro** | ¥2-4（~$0.3-0.6） | **便宜 30-50 倍** |
| 智谱 GLM-4-Plus | ¥5-10（~$0.7-1.4） | **便宜 15-20 倍** |

> 💡 **核心功能不受影响**：Codex 的工具调用、沙盒环境、上下文管理等能力在客户端，不在模型。代理只负责协议转换。

---

## 🚀 快速开始

### 方式一：直接使用（推荐）

#### 1. 克隆项目

```bash
git clone https://github.com/Lucasmantou/codex-proxy.git
cd codex-proxy
```

#### 2. 安装依赖

```bash
pip install -r requirements.txt
```

#### 3. 配置 API Key

**方式 A：环境变量（推荐）**

```bash
# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-xxxxxxxxxxxx"

# Windows CMD
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx

# Linux/Mac
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxx"
```

**方式 B：.env 文件**

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

#### 4. 启动代理

```bash
# DeepSeek（默认）
python codex_proxy.py --upstream https://api.deepseek.com

# 智谱 GLM
python codex_proxy.py --upstream https://open.bigmodel.cn/api/paas/v4
```

#### 5. 配置 Codex

编辑 `~/.codex/config.toml`（参考 `config.toml.example`）：

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

#### 6. 启动 Codex

**⚠️ 重要：先启动代理，再打开 Codex 桌面端。**

---

### 方式二：一键启动脚本（Windows）

1. 编辑 `启动Codex代理.bat`，填入你的 API Key
2. 双击运行
3. 看到 `Running on http://127.0.0.1:9090` 后，打开 Codex

---

## 🔧 配置详解

### 命令行参数

```bash
python codex_proxy.py [选项]
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--upstream` | `https://api.deepseek.com` | 上游 API 地址 |
| `--port` | `9090` | 代理监听端口 |
| `--host` | `127.0.0.1` | 代理监听地址 |

### 模型映射

代理会自动将 Codex 内部使用的 GPT 模型名映射为其他模型：

| Codex 模型名 | DeepSeek 模型 | 智谱模型 |
|-------------|---------------|---------|
| gpt-5.4 | deepseek-v4-pro | glm-4-plus |
| gpt-5.4-mini | deepseek-v4-flash | glm-4-flash |
| gpt-4o | deepseek-v4-pro | glm-4-plus |
| gpt-4o-mini | deepseek-v4-flash | glm-4-flash |

> 💡 如果 Codex 更新后模型名变化，只需修改 `codex_proxy.py` 中的 `MODEL_MAP`。

---

## 🛠️ 技术原理

### 为什么 Codex 不能直接切换模型？

Codex 使用的是 OpenAI **Responses API**，而其他模型使用的是标准 **Chat Completions API**。两者的请求/响应格式完全不同。

**请求格式对比：**

```json
// Codex 发出的请求（Responses API）
{
  "model": "gpt-5.4",
  "input": [{"type": "message", "role": "user", "content": "你好"}],
  "tools": [...]
}

// DeepSeek 期望的请求（Chat Completions API）
{
  "model": "deepseek-v4-pro",
  "messages": [{"role": "user", "content": "你好"}],
  "tools": [...]
}
```

**代理做了什么？**

1. **请求翻译**：将 Responses API 格式转换为 Chat Completions 格式
2. **响应翻译**：将 Chat Completions SSE 转换为 Responses API SSE
3. **思维链存储**：本地维护 `reasoning_store.json`，保存和恢复思维链内容
4. **消息合并**：将 Codex 拆开的 assistant 消息和 function_call 合并成标准格式

> 📖 详细技术分析请参考：[Codex前端为什么不能直接切换模型？](./Codex前端为什么不能直接切换模型？.md)

---

## ❓ 常见问题

<details>
<summary><b>Q1：启动后 Codex 白屏？</b></summary>

**原因：** 代理未启动或启动顺序错误。

**解决：**
1. 确保代理已启动（看到 `Running on http://127.0.0.1:9090`）
2. 清空 `~/.codex/.codex-global-state.json` 中的 `active-workspace-roots` 和 `projectless-thread-ids`
3. 重启 Codex
</details>

<details>
<summary><b>Q2：对话中断，报 400 错误？</b></summary>

**原因：** `reasoning_store.json` 损坏或丢失。

**解决：**
```bash
rm reasoning_store.json
# 重启代理
```
</details>

<details>
<summary><b>Q3：多进程冲突，端口被占用？</b></summary>

**原因：** Windows 上多个 Python 进程可以同时绑定同一端口。

**解决：**
```bash
taskkill //F //IM python.exe
# 重启代理
```
</details>

<details>
<summary><b>Q4：更新 Codex 后配置丢失？</b></summary>

**解决：**
```bash
# 备份配置
cp ~/.codex/config.toml ~/.codex/config.toml.bak
# 更新后恢复
cp ~/.codex/config.toml.bak ~/.codex/config.toml
```
</details>

<details>
<summary><b>Q5：如何切换其他模型？</b></summary>

修改 `codex_proxy.py` 中的 `MODEL_MAP` 和启动时的 `--upstream` 参数：

```python
# 智谱 GLM
MODEL_MAP = {
    "gpt-5.4": "glm-4-plus",
    "gpt-5.4-mini": "glm-4-flash",
}
```
```bash
python codex_proxy.py --upstream https://open.bigmodel.cn/api/paas/v4
```
</details>

<details>
<summary><b>Q6：代理会不会很慢？</b></summary>

**不会。** 代理只做协议转换，延迟增加约 50-100ms，几乎无感。
</details>

更多问题请查看 [详细部署指南](./Codex代理部署指南.md)。

---

## 📁 项目结构

```
codex-proxy/
├── codex_proxy.py          # 代理主程序
├── requirements.txt        # Python 依赖
├── .env.example           # 环境变量模板
├── config.toml.example    # Codex 配置模板
├── 启动Codex代理.bat      # Windows 启动脚本
├── LICENSE                # MIT 许可证
└── README.md              # 本文件
```

---

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！

### 如何贡献

1. **Fork** 本项目
2. 创建特性分支：`git checkout -b feature/amazing-feature`
3. 提交更改：`git commit -m 'Add amazing feature'`
4. 推送到分支：`git push origin feature/amazing-feature`
5. 创建 **Pull Request**

### 开发环境

```bash
# 克隆你的 Fork
git clone https://github.com/Lucasmantou/codex-proxy.git
cd codex-proxy

# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python codex_proxy.py --upstream https://api.deepseek.com
```

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

- [OpenAI](https://openai.com) - Codex 客户端
- [DeepSeek](https://deepseek.com) - 高性价比的 AI 模型
- [智谱 AI](https://zhipuai.cn) - GLM 系列模型

---

## 📧 联系方式

如有问题或建议，请通过以下方式联系：

- 提交 [Issue](https://github.com/Lucasmantou/codex-proxy/issues)
- 微信联系：Lucas_16_1213

---

<p align="center">
  如果觉得有用，请给个 ⭐ Star 支持一下！
</p>
