# Otty 远程工作台：架构与 PWA 计划

当前状态：**Gateway 反向隧道 + 本地主动连接（v0.3）** 已实现并通过 54 项测试，运行方式见 [README.md](README.md)。

## 1. 整体架构

```text
手机 PWA 全屏客户端 (Standalone Web App)
          ↓ HTTPS (/api/v1/...) + WSS (/ws/client) + 实例长 Token
Otty Gateway (gateway.py，公网唯一部署，同时托管 PWA 静态页)
          ↑ 主动 WSS 长连接 (/ws/agent) + 注册密钥
本地 Otty Server (main.py，运行在各台电脑的 Otty 专用 pane 内)
          ↓ 本机 CLI (otty ... --json)
本机 Otty 终端 (各 Tab 内的 Claude / Codex / Shell)
```

## 2. 核心设计原则

1. **工作台所见即所得**：不做复杂的 Agent 协议转换或终端文本正则抠取，直接原样分页呈现终端屏幕（`/screen`），由人眼直观查看。
2. **以 Tab 为操作单位**：
   - `GET /api/v1/tabs`：列出所有 Tab 标题、目录、活动 pane、Agent 及状态（`idle` / `processing` / `awaiting` / `starting`）。
   - `POST /api/v1/tabs` / `DELETE /api/v1/tabs/{id}`：新开与关闭 Tab（保护服务自身所在 Tab）。
   - `GET /api/v1/tabs/{id}/screen`：固定快照游标分页读取终端内容（默认最新 50 行，剥离底部空行）。
   - `POST /api/v1/tabs/{id}/messages`：通过 stdin 原样发送消息并提交（`awaiting` 时返回 409 防止死锁排队）。
   - `POST /api/v1/tabs/{id}/keys`：发送白名单按键（`Enter`、`Up`、`Down`、`Esc`、`1`、`2`、`Ctrl+C` 等）回应 Agent 审批菜单。
   - `GET /api/v1/tabs/{id}/wait`：基于 `otty pane wait` 的长轮询等待空闲。
3. **全屏 PWA 客户端（下一步实现）**：
   - 直接由 `gateway.py` 托管在 `/` 根路径，零前端编译依赖、同源零跨域。
   - 开启 PWA `display: standalone` + 锁死视口滚动（`position: fixed; overscroll-behavior: none`），添加到手机主屏幕后彻底消除浏览器顶底栏、左滑误退和下拉刷新。
   - `localStorage` 持久化 Token、当前选中 Tab、输入草稿与常用目录，切后台返回自动重连 `/ws/client` 并刷新屏幕。
