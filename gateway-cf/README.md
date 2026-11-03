# Otty Gateway — Cloudflare Workers + Durable Objects（免费版）

本目录提供与 Python 版 [`gateway/`](../gateway/) **协议 100% 兼容** 的 Cloudflare Workers + Durable Objects（SQLite 免费版）实现，并且直接复用 [`../gateway/pwa/`](../gateway/pwa/) 下的前端 PWA 静态文件。

## 一键部署步骤

### 1. 修改注册密钥（可选）
编辑 [`wrangler.toml`](./wrangler.toml) 中的 `OTTY_GATEWAY_REGISTER_KEY`（至少 16 位字符），或者部署后用 `wrangler secret` 设置。

### 2. 登录并部署到 Cloudflare 免费账号
```bash
cd gateway-cf

# 1. 浏览器登录 Cloudflare 账号
npx wrangler login

# 2. 自动创建 SQLite Durable Object 并部署上线
npx wrangler deploy
```

部署成功后，终端会输出类似下面的公网地址：
```text
https://otty-gateway.<你的子域名>.workers.dev
```

### 3. 本地 Mac 连接该 Cloudflare Gateway
在本地 Otty 面板中运行根目录的 `main.py`：
```bash
OTTY_GATEWAY_URL="https://otty-gateway.<你的子域名>.workers.dev" \
OTTY_GATEWAY_REGISTER_KEY="otty-local-gateway-reg-key-2026" \
.venv/bin/python main.py
```

启动后直接用手机打开终端里打印出的 `PWA Client` 链接即可使用。

## 语音输入（按住说话）

PWA 输入栏左侧的 🎤 按钮切换为「按住 说话」，松手后录音会上传到 `POST /api/v1/asr`（需 Bearer Token，且 Token 对应的本地实例必须在线），调用阿里云百炼 `qwen-audio-3.1-asr-flash` 识别，结果填入输入框，修改后再发送。

| 变量 | 说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` | **Secret**，阿里云百炼 API Key（后台 Settings → Variables and Secrets 添加，或 `npx wrangler secret put DASHSCOPE_API_KEY`） |
| `OTTY_ASR_MODEL` | 可选，默认 `qwen-audio-3.1-asr-flash` |
| `OTTY_ASR_ENDPOINT` | 可选，默认 `https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`，也可填 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/...` |

Worker 未配置 `DASHSCOPE_API_KEY` 时，请求会转发给本地 Otty Server，由本地 `.env` 中的同名变量完成识别。
