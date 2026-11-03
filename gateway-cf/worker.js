const MIN_SECRET_LENGTH = 16;
const STATIC_FILES = new Set([
  "/index.html",
  "/manifest.json",
  "/sw.js",
  "/icon.svg",
  "/styles.css",
  "/app.js",
]);

const NO_CACHE_HEADERS = {
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  Pragma: "no-cache",
  Expires: "0",
};

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function extractBearer(authHeader) {
  if (!authHeader) return "";
  const idx = authHeader.indexOf(" ");
  if (idx === -1) return "";
  const scheme = authHeader.slice(0, idx).toLowerCase();
  if (scheme !== "bearer") return "";
  return authHeader.slice(idx + 1).trim();
}

function extractToken(request, url) {
  return (
    extractBearer(request.headers.get("Authorization")) ||
    url.searchParams.get("token") ||
    ""
  ).trim();
}

function buildCorsHeaders(request, env) {
  const raw = (env.OTTY_GATEWAY_CORS_ORIGINS || "").trim();
  if (!raw) return {};
  const allowed = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const origin = request.headers.get("Origin") || "";
  if (allowed.includes("*") || allowed.includes(origin)) {
    return {
      "Access-Control-Allow-Origin": origin || "*",
      "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Instance-Id",
    };
  }
  return {};
}

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function closeWebSocketWithReason(code, reason) {
  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();
  server.close(code, reason);
  return new Response(null, { status: 101, webSocket: client });
}

function normalizePath(pathname) {
  let path = pathname || "/";
  if (path === "/otty" || path === "/otty/") return "/";
  if (path.startsWith("/otty/")) {
    path = path.slice("/otty".length);
  }
  if (path === "/pwa" || path === "/pwa/") return "/";
  if (path.startsWith("/pwa/")) {
    path = path.slice("/pwa".length);
  }
  return path || "/";
}

// ---------------------------------------------------------------------------
// 语音输入（按住说话）：调用阿里云百炼 Qwen-Audio-ASR 同步识别
// 密钥通过 Worker 环境变量 / Secret 配置：DASHSCOPE_API_KEY
// 可选：OTTY_ASR_MODEL（默认 qwen-audio-3.1-asr-flash）、OTTY_ASR_ENDPOINT
// ---------------------------------------------------------------------------
const DEFAULT_ASR_MODEL = "qwen-audio-3.1-asr-flash";
const DEFAULT_ASR_ENDPOINT =
  "https://llm-6b7p48qstpbrqpqn.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation";
const MAX_AUDIO_BYTES = 8 * 1024 * 1024;
const AUDIO_MIME = {
  wav: "audio/wav",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  aac: "audio/aac",
  ogg: "audio/ogg",
  webm: "audio/webm",
};

function validateAsrBody(body) {
  if (!body || typeof body !== "object") return "Request body must be a JSON object";
  let audio = typeof body.audio === "string" ? body.audio : "";
  if (audio.startsWith("data:")) audio = audio.split(",", 2)[1] || "";
  if (audio.length < 16) return "audio is required (base64)";
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(audio)) return "audio must be valid base64";
  if ((audio.length * 3) / 4 > MAX_AUDIO_BYTES) return "audio is too large";
  const format = body.format || "wav";
  if (!AUDIO_MIME[format]) return `Unsupported audio format '${format}'`;
  const sampleRate = Number(body.sample_rate || 16000);
  if (!Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000) {
    return "sample_rate must be an integer between 8000 and 48000";
  }
  body.audio = audio;
  body.format = format;
  body.sample_rate = sampleRate;
  return null;
}

function extractAsrText(payload) {
  const output = payload && typeof payload === "object" ? payload.output || {} : {};
  if (typeof output.text === "string") return output.text;
  if (output.sentence && typeof output.sentence.text === "string") return output.sentence.text;
  const content = output.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => p?.text || "").join("");
  return "";
}

async function transcribeWithDashScope(env, body) {
  const model = (env.OTTY_ASR_MODEL || DEFAULT_ASR_MODEL).trim();
  const endpoint = (env.OTTY_ASR_ENDPOINT || DEFAULT_ASR_ENDPOINT).trim();
  const mime = AUDIO_MIME[body.format] || "audio/wav";
  let res;
  try {
    res = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.DASHSCOPE_API_KEY.trim()}`,
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
      },
      body: JSON.stringify({
        model,
        input: {
          messages: [
            {
              role: "user",
              content: [
                {
                  type: "input_audio",
                  input_audio: { data: `data:${mime};base64,${body.audio}` },
                },
              ],
            },
          ],
        },
        parameters: { format: body.format, sample_rate: String(body.sample_rate) },
      }),
    });
  } catch (err) {
    return { status: 502, body: { detail: `ASR service unreachable: ${err}` } };
  }
  let payload = {};
  try {
    payload = await res.json();
  } catch {
    payload = {};
  }
  if (!res.ok) {
    return {
      status: 502,
      body: { detail: `ASR failed (${res.status}): ${payload?.message || "unknown error"}` },
    };
  }
  return { status: 200, body: { text: extractAsrText(payload).trim(), model } };
}

export class OttyInstanceHub {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.agentWs = null;
    this.instanceId = null;
    this.tokenHash = null;
    this.lastTabs = null;
    this.subscribers = new Set();
    this.pending = new Map();
  }

  broadcast(payload) {
    const raw = JSON.stringify(payload);
    for (const ws of Array.from(this.subscribers)) {
      try {
        ws.send(raw);
      } catch {
        this.subscribers.delete(ws);
      }
    }
  }

  rejectAllPending(reason = "Local Otty server disconnected") {
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.resolve({ status: 503, body: { detail: reason } });
    }
    this.pending.clear();
  }

  forwardToAgent(method, path, query = "", body = null) {
    if (!this.agentWs) {
      return Promise.resolve({
        status: 503,
        body: { detail: "No online instance matches this bearer token" },
      });
    }
    const reqId = crypto.randomUUID().replace(/-/g, "");
    const timeoutMs = path.endsWith("/wait") || path.endsWith("/asr") ? 75000 : 20000;

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(reqId);
        resolve({
          status: 504,
          body: { detail: "Timed out waiting for local Otty server response" },
        });
      }, timeoutMs);

      this.pending.set(reqId, { resolve, timer });
      try {
        this.agentWs.send(
          JSON.stringify({
            type: "request",
            id: reqId,
            method: method.toUpperCase(),
            path,
            query,
            body,
          })
        );
      } catch {
        clearTimeout(timer);
        this.pending.delete(reqId);
        resolve({
          status: 503,
          body: { detail: "Local Otty server disconnected" },
        });
      }
    });
  }

  handleAgentUpgrade(request, url, tokenHash) {
    const expectedKey = (this.env.OTTY_GATEWAY_REGISTER_KEY || "").trim();
    const regKey = (
      request.headers.get("X-Register-Key") ||
      url.searchParams.get("register_key") ||
      ""
    ).trim();

    if (!expectedKey || regKey !== expectedKey) {
      return closeWebSocketWithReason(4401, "Invalid register key");
    }

    const instanceId = (
      request.headers.get("X-Instance-Id") ||
      url.searchParams.get("instance_id") ||
      `inst_${tokenHash.slice(0, 12)}`
    ).trim();

    if (this.agentWs) {
      const prev = this.agentWs;
      this.agentWs = null;
      this.rejectAllPending("Replaced by a newer connection");
      try {
        prev.close(4009, "Replaced by a newer connection");
      } catch {
        // Ignore
      }
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();

    this.agentWs = server;
    this.instanceId = instanceId;
    this.tokenHash = tokenHash;

    server.send(
      JSON.stringify({
        type: "registered",
        instance_id: instanceId,
        subscribers: this.subscribers.size,
      })
    );
    this.broadcast({
      type: "event",
      event: "agent.online",
      instance_id: instanceId,
    });

    server.addEventListener("message", (event) => {
      try {
        const msg = JSON.parse(
          typeof event.data === "string"
            ? event.data
            : new TextDecoder().decode(event.data)
        );
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "response") {
          const entry = this.pending.get(String(msg.id || ""));
          if (entry) {
            clearTimeout(entry.timer);
            this.pending.delete(String(msg.id || ""));
            entry.resolve({
              status: Number(msg.status || 502),
              body: msg.body,
            });
          }
        } else if (msg.type === "event") {
          if (msg.event === "tabs.updated" && Array.isArray(msg.tabs)) {
            this.lastTabs = msg.tabs;
          }
          this.broadcast({
            ...msg,
            type: "event",
            instance_id: instanceId,
          });
        } else if (msg.type === "ping") {
          server.send(JSON.stringify({ type: "pong" }));
        }
      } catch {
        // Ignore malformed frame
      }
    });

    const onDisconnect = () => {
      if (this.agentWs === server) {
        this.agentWs = null;
        this.rejectAllPending("Local Otty server disconnected");
        this.broadcast({
          type: "event",
          event: "agent.offline",
          instance_id: instanceId,
        });
      }
    };
    server.addEventListener("close", onDisconnect);
    server.addEventListener("error", onDisconnect);

    return new Response(null, { status: 101, webSocket: client });
  }

  handleClientUpgrade(requestedInstanceId) {
    if (!this.agentWs) {
      return closeWebSocketWithReason(
        4401,
        "No online instance matches this bearer token"
      );
    }
    if (requestedInstanceId && requestedInstanceId !== this.instanceId) {
      return closeWebSocketWithReason(
        4503,
        `Instance '${requestedInstanceId}' is offline`
      );
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();

    this.subscribers.add(server);
    try {
      this.agentWs.send(
        JSON.stringify({ type: "subscribers", count: this.subscribers.size })
      );
    } catch {
      // Ignore
    }

    server.send(
      JSON.stringify({
        type: "connected",
        instance_id: this.instanceId,
        online: true,
        tabs: this.lastTabs,
      })
    );

    server.addEventListener("message", async (event) => {
      try {
        const msg = JSON.parse(
          typeof event.data === "string"
            ? event.data
            : new TextDecoder().decode(event.data)
        );
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "ping") {
          server.send(JSON.stringify({ type: "pong" }));
        } else if (msg.type === "request") {
          const reqId = msg.id;
          const method = String(msg.method || "GET").toUpperCase();
          let path = String(msg.path || "/api/v1/tabs");
          if (!path.startsWith("/api/v1/")) {
            path = "/api/v1/" + path.replace(/^\/+/, "");
          }
          let query = String(msg.query || "");
          if (msg.params && typeof msg.params === "object") {
            query = new URLSearchParams(msg.params).toString();
          }
          const { status, body } = await this.forwardToAgent(
            method,
            path,
            query,
            msg.body ?? null
          );
          server.send(
            JSON.stringify({
              type: "response",
              id: reqId,
              status,
              body,
            })
          );
        }
      } catch {
        // Ignore malformed frame
      }
    });

    const onClose = () => {
      this.subscribers.delete(server);
      if (this.agentWs) {
        try {
          this.agentWs.send(
            JSON.stringify({ type: "subscribers", count: this.subscribers.size })
          );
        } catch {
          // Ignore
        }
      }
    };
    server.addEventListener("close", onClose);
    server.addEventListener("error", onClose);

    return new Response(null, { status: 101, webSocket: client });
  }

  async fetch(request) {
    const url = new URL(request.url);
    const cors = buildCorsHeaders(request, this.env);
    const mode = request.headers.get("X-Otty-Mode");
    const tokenHash = request.headers.get("X-Otty-Token-Hash") || "";
    const requestedInstanceId =
      request.headers.get("X-Otty-Instance-Id") ||
      url.searchParams.get("instance_id") ||
      "";

    if (mode === "ws-agent") {
      return this.handleAgentUpgrade(request, url, tokenHash);
    }

    if (mode === "ws-client") {
      return this.handleClientUpgrade(requestedInstanceId);
    }

    if (mode === "http-proxy") {
      if (!this.agentWs) {
        return jsonResponse(
          { detail: "No online instance matches this bearer token" },
          401,
          { "WWW-Authenticate": "Bearer", ...cors }
        );
      }
      if (requestedInstanceId && requestedInstanceId !== this.instanceId) {
        return jsonResponse(
          { detail: `Instance '${requestedInstanceId}' is offline` },
          503,
          cors
        );
      }

      let body = null;
      const raw = await request.text();
      if (raw) {
        try {
          body = JSON.parse(raw);
        } catch {
          return jsonResponse(
            { detail: "Request body must be valid JSON" },
            400,
            cors
          );
        }
      }

      const apiPath = request.headers.get("X-Otty-Api-Path") || "/api/v1/tabs";

      // 语音识别：Token 已通过上方在线实例校验，Gateway 配置了密钥则直接识别，
      // 否则转发给本地 Otty Server（使用本地环境变量里的密钥）。
      if (apiPath === "/api/v1/asr") {
        if (request.method !== "POST") {
          return jsonResponse({ detail: "Method Not Allowed" }, 405, cors);
        }
        const invalid = validateAsrBody(body);
        if (invalid) {
          return jsonResponse({ detail: invalid }, 422, cors);
        }
        if ((this.env.DASHSCOPE_API_KEY || "").trim()) {
          const { status, body: asrBody } = await transcribeWithDashScope(this.env, body);
          return jsonResponse(asrBody, status, cors);
        }
      }

      const query = url.search ? url.search.slice(1) : "";
      const { status, body: replyBody } = await this.forwardToAgent(
        request.method,
        apiPath,
        query,
        body
      );
      return jsonResponse(replyBody, status, cors);
    }

    return jsonResponse({ detail: "Not Found" }, 404, cors);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = buildCorsHeaders(request, env);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    const path = normalizePath(url.pathname);

    // 1. Gateway 健康检查
    if (path === "/gateway/health") {
      return jsonResponse({ status: "ok", runtime: "cloudflare-workers" }, 200, cors);
    }

    // 2. 静态 PWA 页面与资源（支持 /、/otty/、/pwa/ 等路径）
    const assetPath = path === "/" ? "/index.html" : path;
    if (STATIC_FILES.has(assetPath) && env.ASSETS) {
      const assetUrl = new URL(request.url);
      assetUrl.pathname = assetPath;
      const res = await env.ASSETS.fetch(new Request(assetUrl.toString(), request));
      const headers = new Headers(res.headers);
      if (assetPath !== "/icon.svg") {
        for (const [k, v] of Object.entries(NO_CACHE_HEADERS)) {
          headers.set(k, v);
        }
      }
      return new Response(res.body, { status: res.status, headers });
    }

    // 3. 解析并校验 Token，计算路由到固定 Durable Object 的哈希
    const token = extractToken(request, url);
    const isWsUpgrade =
      (request.headers.get("Upgrade") || "").toLowerCase() === "websocket";

    if (path === "/ws/agent") {
      if (!isWsUpgrade) {
        return jsonResponse({ detail: "Expected WebSocket upgrade" }, 426, cors);
      }
      if (token.length < MIN_SECRET_LENGTH) {
        return closeWebSocketWithReason(4401, "Invalid instance token");
      }
      const tokenHash = await sha256Hex(token);
      const id = env.OTTY_HUB.idFromName(tokenHash);
      const headers = new Headers(request.headers);
      headers.set("X-Otty-Mode", "ws-agent");
      headers.set("X-Otty-Token-Hash", tokenHash);
      return env.OTTY_HUB.get(id).fetch(new Request(request.url, { headers }));
    }

    // 客户端 WebSocket: /ws/client 或 /i/{instance_id}/ws
    const clientWsMatch = path.match(/^\/i\/([^/]+)\/ws$/);
    if (path === "/ws/client" || clientWsMatch) {
      if (!isWsUpgrade) {
        return jsonResponse({ detail: "Expected WebSocket upgrade" }, 426, cors);
      }
      if (token.length < MIN_SECRET_LENGTH) {
        return closeWebSocketWithReason(4401, "Invalid or missing bearer token");
      }
      const tokenHash = await sha256Hex(token);
      const id = env.OTTY_HUB.idFromName(tokenHash);
      const headers = new Headers(request.headers);
      headers.set("X-Otty-Mode", "ws-client");
      headers.set("X-Otty-Token-Hash", tokenHash);
      if (clientWsMatch) {
        headers.set("X-Otty-Instance-Id", decodeURIComponent(clientWsMatch[1]));
      }
      return env.OTTY_HUB.get(id).fetch(new Request(request.url, { headers }));
    }

    // 客户端 HTTP 转发: /api/v1/* 或 /i/{instance_id}/api/v1/*
    const instApiMatch = path.match(/^\/i\/([^/]+)(\/api\/v1\/.*)$/);
    const directApiMatch = path.match(/^(\/api\/v1\/.*)$/);
    if (instApiMatch || directApiMatch) {
      if (!token) {
        return jsonResponse(
          { detail: "Invalid or missing bearer token" },
          401,
          { "WWW-Authenticate": "Bearer", ...cors }
        );
      }
      const tokenHash = await sha256Hex(token);
      const id = env.OTTY_HUB.idFromName(tokenHash);
      const headers = new Headers(request.headers);
      headers.set("X-Otty-Mode", "http-proxy");
      headers.set("X-Otty-Token-Hash", tokenHash);
      headers.set(
        "X-Otty-Api-Path",
        instApiMatch ? instApiMatch[2] : directApiMatch[1]
      );
      if (instApiMatch) {
        headers.set("X-Otty-Instance-Id", decodeURIComponent(instApiMatch[1]));
      } else if (request.headers.get("X-Instance-Id")) {
        headers.set("X-Otty-Instance-Id", request.headers.get("X-Instance-Id"));
      }
      return env.OTTY_HUB.get(id).fetch(
        new Request(request.url, {
          method: request.method,
          headers,
          body:
            request.method === "GET" || request.method === "HEAD"
              ? null
              : request.body,
        })
      );
    }

    return jsonResponse({ detail: "Not Found" }, 404, cors);
  },
};
