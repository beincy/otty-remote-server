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

## MCP：多实例组路由

MCP 地址为 `https://你的Gateway域名/mcp`，使用 Streamable HTTP，无需新增端口或 Server → Gateway 连接。
使用 Node.js 22+，在 `gateway-cf` 执行 `npm ci`、`npm run deploy`，并重启更新后的本地 `main.py`。
本功能由 Cloudflare Gateway 提供；Python Gateway（`../gateway/`）已同步同一套逻辑（`/mcp` 端点、组路由、权限、幂等写回执，持久化见 `OTTY_GATEWAY_MCP_STORE`），纯本地 HTTP 模式暂不提供 MCP。

### 默认启动

Server 只保留原来的实例 Token，不生成额外的本地 MCP Token。
首次启动会创建 `~/.config/otty-server/mcp-groups.json`：默认组以现有实例 Token 为 key，成员只有自身。
已有文件会保留。支持 `OTTY_MCP_ROUTES_FILE` 指定路径；默认与实例凭证文件处于同一目录。
启动时打印：

```text
MCP URL     : https://你的Gateway域名/mcp
MCP Routes  : /Users/you/.config/otty-server/mcp-groups.json
```

不再需要 `OTTY_MCP_CLIENTS` Secret 或凭证生成命令；早期版本的 `mcp-<摘要>.json` 文件不再使用。

### 本地组路由表

每个 key 是客户端请求时携带的 MCP 组 Token，`members` 填成员实例原有的注册 Token。
例如，选择一个已有实例 Token 作为组 key，再把需要一起访问的实例加入成员：

```json
{
  "groups": {
    "otty_replace_with_group_key": {
      "timestamp": 1,
      "permissions": ["read", "control", "manage"],
      "members": [
        "otty_replace_with_instance_A_token",
        "otty_replace_with_instance_B_token"
      ]
    }
  }
}
```

示例 key 和成员需要替换成实际值。key 和 Token 长度为 16–512 字符。
可以添加多个 key，每个 key 的组成员和权限彼此独立；每组最多 100 个成员，每份表最多 100 个 key。
文件包含访问凭证，Server 创建和保存时使用 `0600` 权限。
成员实例 Token 轮换后，需要在路由表中更新该成员并增大时间戳。

同步规则：

- Server 注册 / 重连后通过原有 WS 上传整份表，并订阅本地表中的 key。
- Gateway 对相同 key 比较 `timestamp`，只接受更大的版本，整条替换；相同或更旧的忽略。
- Gateway 返回最新版本，并向订阅该 key 的 Server 推送变更；Server 将较新版本保存到本地文件。
- 本地文件默认每 2 秒检查一次，修改后自动上传，不必重启。修改某条配置时必须增大其 `timestamp`，建议使用毫秒时间戳。
- 要清空组成员，用更大的时间戳上传 `"members": []`。从本地表删除 key 只会停止订阅，不会删除 Gateway 中已有的组。
- Gateway 组路由表和订阅信息存储在 SQLite Durable Object 中，Worker / DO 重启后仍保留；离线订阅者的旧连接通知会被忽略，重连后补齐最新配置。

### 权限校验

Gateway 根据组 key 的最新配置过滤 `tools/list`，每次实际工具调用也检查权限：

| 权限 | 工具 |
| --- | --- |
| `read`（必选） | `list_tabs`、`inspect_tab`、`wait_tab` |
| `control` | `send_message`、`send_keys`、`interrupt_tab` |
| `manage` | `open_tab`、`close_tab` |

Server 还有独立的本地权限上限，在 `.env` 中设置：

```dotenv
# 默认开启；设为 0 后重启，拒绝本实例的全部 MCP 操作
OTTY_MCP_ENABLED=1
# 默认 read,control,manage；设为 read 可限制本实例为只读
OTTY_MCP_PERMISSIONS=read,control,manage
```

**组权限不能突破本地权限上限。** Gateway 转发前检查本地声明的权限，Server 收到 MCP 请求后也独立检查，
即使 Gateway 错误放行仍会被本地拦截。环境变量变更后需要重启；PWA / 普通 HTTP 接口继续使用现有实例认证与限制。
宿主 Tab 保护、普通 Shell 输入开关仍然有效；按键可能批准终端中的操作，`control` 应按此能力授权。

### 客户端配置与工具调用

```json
{
  "mcpServers": {
    "otty": {
      "url": "https://你的Gateway域名/mcp",
      "headers": { "Authorization": "Bearer <本地路由表中的组key>" }
    }
  }
}
```

MCP 只接受 Authorization Bearer Header，不接受 URL Token。当前不实现 OAuth 登录或授权发现；客户端需要支持自定义 Header。
Origin 默认只接受同源或不携带 Origin 的请求；外部浏览器客户端需配置 `OTTY_MCP_ALLOWED_ORIGINS` 为精确 Origin 列表，不支持 `*`。
PWA 的 CORS 配置不会放宽 MCP 的 Origin 校验。

`list_tabs` 一次并行获取组内所有在线、已开启 MCP 的实例，按实例 ID 分组返回；离线实例会忽略：

```json
{
  "instances": {
    "inst_a": { "tabs": [{ "id": "t_a", "pane_id": "p_a", "cwd": "/projects/foo" }] },
    "inst_b": { "tabs": [{ "id": "t_b", "pane_id": "p_b", "cwd": "/projects/bar" }] }
  }
}
```

已有 Tab 的工具只需提供 `tab_id`，Gateway 每次在组内反查并转发，不维护 Tab 到实例的映射。
`open_tab` 因为尚无 Tab，需要提供目标 `instance_id`、绝对路径 `cwd`，可选预设 `agent=codex/claude/shell`，不接受任意启动命令。
输入工具还必须提供刚读取的 `expected_pane_id`；本地发现活动 pane 已变化时拒绝输入。

`inspect_tab` 支持有限输出和历史游标；终端输出可能补充启发式匹配的本地会话历史，当前交互以最新终端为准。
终端内容是非可信数据。`wait_tab` 最多等待 25 秒，返回状态和输出；`idle` 不表示任务成功，等待超时不停止 Agent。
`send_message` 的 `queue` 为排队，`now` 为立即引导，回执只表示提交结果。

所有写工具需要 `request_id`（8–128 位字母、数字、下划线或连字符）：
同组在 24 小时内用相同 ID 和参数重试会回放原回执；同 ID 改参数或工具会报冲突。
回执持久化于 SQLite，每组最多 256 个未过期回执。预留后未完成或网关超时的不确定操作不会自动重发，
应先检查目标再决定是否用新 ID 重试；超过 24 小时不要复用旧 ID。

### 验证

```bash
npm test
# 真实 Worker + SQLite DO、多实例模拟隧道、重启持久化恢复，不控制真实 Otty
npm run test:runtime
npx wrangler deploy --dry-run
```
