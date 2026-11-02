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
