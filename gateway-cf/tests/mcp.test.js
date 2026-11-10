import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { OttyInstanceHub, OttyMcpRegistry } from "../worker.js";
import { digest } from "../mcp.js";
import { internalRequest } from "../mcp-registry.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const KEY = "group-key-test-0123456789";
const A = "instance-token-A-0123456789";
const B = "instance-token-B-0123456789";
const C = "offline-instance-token-0123456789";
const ALL = ["read", "control", "manage"];

class Storage {
  values = new Map();
  tail = Promise.resolve();
  async get(key) { return structuredClone(this.values.get(key)); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async delete(key) { return this.values.delete(key); }
  async list({ prefix }) { return new Map([...this.values].filter(([key]) => key.startsWith(prefix))); }
  transaction(callback) {
    const result = this.tail.then(() => callback(this));
    this.tail = result.catch(() => {});
    return result;
  }
}

async function fixture(permissions = ALL) {
  const calls = [];
  const storage = new Storage();
  const hubs = new Map();
  let registry;
  const env = {
    OTTY_MCP: { idFromName: (name) => name, get: () => registry },
    OTTY_HUB: { idFromName: (name) => name, get: (hash) => {
      if (!hubs.has(hash)) hubs.set(hash, new OttyInstanceHub({ storage: new Storage(), waitUntil() {} }, env));
      return hubs.get(hash);
    } },
  };
  registry = new OttyMcpRegistry({ storage }, env);
  const group = { timestamp: 1, permissions, members: [A, B, C] };
  async function add(token, id, tabId) {
    const hash = await digest(token);
    const hub = env.OTTY_HUB.get(hash);
    hub.tokenHash = hash;
    hub.instanceId = id;
    hub.connectionId = `connection-${id}`;
    const state = { hub, events: [], tabs: [{ id: tabId, pane_id: `p_${id}`, agent: "codex", agent_state: "idle", writable: true }], responder: null };
    hub.agentWs = { send(raw) {
      const msg = JSON.parse(raw);
      if (msg.type !== "request") { state.events.push(msg); return; }
      calls.push({ instance: id, ...msg });
      queueMicrotask(async () => {
        let body;
        if (state.responder) body = await state.responder(msg);
        else if (msg.path === "/api/v1/health") body = { capabilities: { expected_pane_id: true } };
        else if (msg.path.endsWith("/screen")) body = { lines: [id], hasMore: false, nextBefore: null };
        else if (msg.path.endsWith("/wait")) body = { done: true, tab: state.tabs[0] };
        else if (msg.method === "POST" || msg.method === "DELETE") body = { tab_id: tabId, pane_id: `p_${id}`, receipt: { queued: true } };
        else body = msg.path === "/api/v1/tabs" ? { tabs: state.tabs } : state.tabs.find((t) => msg.path.endsWith(t.id));
        const pending = hub.pending.get(msg.id);
        clearTimeout(pending.timer);
        hub.pending.delete(msg.id);
        pending.resolve(body?.__status ? { status: body.__status, body: { detail: "test failure" } } : { status: 200, body });
      });
    } };
    await hub.configureMcp({ version: 2, enabled: true, permissions: ALL, groups: { [KEY]: group } });
    return state;
  }
  const a = await add(A, "inst_a", "t_a");
  const b = await add(B, "inst_b", "t_b");
  async function request(body, options = {}) {
    const { method = "POST", token = KEY, headers = {}, path = "/mcp" } = options;
    return worker.fetch(new Request(`https://otty.example${path}`, {
      method, headers: { Accept: "application/json, text/event-stream", "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
      ...(method === "POST" ? { body: typeof body === "string" ? body : JSON.stringify(body) } : {}),
    }), env);
  }
  async function rpc(method, params = {}, options) {
    const result = await request({ jsonrpc: "2.0", id: 1, method, params }, options);
    return { response: result, body: await result.json() };
  }
  const configure = (state, groups, extra = {}) => state.hub.configureMcp({ version: 2, enabled: true, permissions: ALL, groups, ...extra });
  return { env, calls, storage, a, b, group, request, rpc, configure,
    restartRegistry() { registry = new OttyMcpRegistry({ storage }, env); },
  };
}
const args = { tab_id: "t_a", expected_pane_id: "p_inst_a", text: "hello", request_id: "request_123" };
const write = (f, values = args) => f.rpc("tools/call", { name: "send_message", arguments: values });

test("one list returns every online member's tabs grouped by instance; offline members are ignored", async () => {
  const f = await fixture();
  const result = await f.rpc("tools/call", { name: "list_tabs" });
  assert.deepEqual(Object.keys(result.body.result.structuredContent.instances), ["inst_a", "inst_b"]);
  assert.equal(result.body.result.structuredContent.instances.inst_b.tabs[0].id, "t_b");
  assert.equal(f.calls.length, 2);
  assert.ok(f.calls.every((call) => call.source === "mcp"));
  assert.ok(!JSON.stringify(result.body).includes(A));
});

test("every operation resolves tab_id anew inside the group, without cached routing", async () => {
  const f = await fixture();
  assert.equal((await write(f)).body.result.isError, false);
  assert.equal(f.calls.at(-1).instance, "inst_a");
  f.a.tabs = [];
  f.b.tabs = [{ ...f.b.tabs[0], id: "t_a" }];
  const inspect = await f.rpc("tools/call", { name: "inspect_tab", arguments: { tab_id: "t_a" } });
  assert.deepEqual(inspect.body.result.structuredContent.screen.lines, ["inst_b"]);
});

test("unknown group keys, URL tokens and forged internal headers cannot authenticate", async () => {
  const f = await fixture();
  for (const token of [null, "unknown-token-0123456789"]) for (const method of ["POST", "GET", "DELETE"]) {
    const result = await f.request({}, { token, method, path: `/mcp?token=${KEY}`, headers: {
      "X-Otty-Mode": "mcp-publish", "X-Otty-Mcp-Key": await digest(KEY), "X-Otty-Mcp-Policy": '{"permissions":["manage"]}',
    } });
    assert.equal(result.status, 401);
  }
  assert.equal(f.calls.length, 0);
});

test("group membership limits tab lookup even when another instance is online", async () => {
  const f = await fixture();
  await f.configure(f.a, { [KEY]: { ...f.group, timestamp: 2, members: [A] } });
  const denied = await f.rpc("tools/call", { name: "inspect_tab", arguments: { tab_id: "t_b" } });
  assert.equal(denied.body.result.structuredContent.http_status, 404);
  assert.ok(!f.calls.some((c) => c.instance === "inst_b"));
});

test("Origin enforcement includes preflight and is independent of permissive PWA CORS", async () => {
  const f = await fixture();
  f.env.OTTY_GATEWAY_CORS_ORIGINS = "*";
  for (const method of ["POST", "OPTIONS"]) assert.equal((await f.request({}, { method, headers: { Origin: "https://evil.test" } })).status, 403);
  f.env.OTTY_MCP_ALLOWED_ORIGINS = "https://client.test";
  const preflight = await f.request({}, { method: "OPTIONS", token: null, headers: { Origin: "https://client.test" } });
  assert.equal(preflight.status, 204);
  assert.equal(preflight.headers.get("Access-Control-Allow-Origin"), "https://client.test");
});

test("Gateway hides forbidden tools and checks permissions again at invocation", async () => {
  const f = await fixture(["read"]);
  assert.equal((await f.rpc("tools/list")).body.result.tools.length, 3);
  const denied = await write(f);
  assert.ok(denied.body.error || denied.body.result.isError);
  assert.equal(f.calls.length, 0);
});

test("local permission ceiling blocks writes even if the group allows them; PWA remains usable", async () => {
  const f = await fixture();
  await f.configure(f.a, { [KEY]: f.group }, { permissions: ["read"] });
  const result = await write(f);
  assert.equal(result.body.result.structuredContent.http_status, 403);
  assert.ok(!f.calls.some((c) => c.method === "POST"));
  const pwa = await f.request({}, { method: "GET", token: A, path: "/api/v1/tabs" });
  assert.equal(pwa.status, 200);
  assert.equal(f.calls.at(-1).source, undefined);
});

test("local disable removes an instance from MCP results and does not disable another member", async () => {
  const f = await fixture();
  await f.configure(f.a, { [KEY]: f.group }, { enabled: false });
  const result = await f.rpc("tools/call", { name: "list_tabs" });
  assert.deepEqual(Object.keys(result.body.result.structuredContent.instances), ["inst_b"]);
});

test("newest timestamp replaces the entire group and notifies all subscribers; stale/equal versions lose", async () => {
  const f = await fixture();
  const newer = { ...f.group, timestamp: 10, members: [B], permissions: ["read"] };
  await f.configure(f.b, { [KEY]: newer });
  assert.deepEqual(f.a.events.at(-1).groups[KEY], newer);
  const events = f.a.events.length;
  const reply = await f.configure(f.a, { [KEY]: f.group });
  assert.deepEqual(reply[KEY], newer);
  await f.configure(f.a, { [KEY]: { ...f.group, timestamp: 10 } });
  assert.equal(f.a.events.length, events);
  assert.equal((await f.rpc("tools/list")).body.result.tools.length, 3);
  assert.deepEqual((await f.rpc("tools/call", { name: "list_tabs" })).body.result.structuredContent.instances.inst_b.tabs[0].id, "t_b");
});

test("an unsubscribed key is not pushed, and clearing members survives old uploads", async () => {
  const f = await fixture();
  await f.configure(f.a, {});
  const count = f.a.events.length;
  await f.configure(f.b, { [KEY]: { ...f.group, timestamp: 5, members: [] } });
  assert.equal(f.a.events.length, count);
  await f.configure(f.a, { [KEY]: f.group });
  assert.deepEqual((await f.rpc("tools/call", { name: "list_tabs" })).body.result.structuredContent.instances, {});
});

test("registry recreation retains groups, timestamps, subscribers and write receipts", async () => {
  const f = await fixture();
  const first = await write(f);
  f.restartRegistry();
  assert.deepEqual((await write(f)).body, first.body);
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
  await f.configure(f.b, { [KEY]: { ...f.group, timestamp: 4 } });
  assert.equal(f.a.events.at(-1).groups[KEY].timestamp, 4);
});

test("writes deduplicate concurrently and conflicting reuse never executes", async () => {
  const f = await fixture();
  const results = await Promise.all(Array.from({ length: 4 }, () => write(f)));
  assert.ok(results.some((r) => r.body.result.isError === false));
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
  const conflict = await write(f, { ...args, text: "changed" });
  assert.equal(conflict.body.result.structuredContent.code, "request_id_conflict");
});

test("uncertain results are replayed without repeating the write", async () => {
  const f = await fixture();
  f.a.responder = (msg) => msg.path.endsWith("/messages") ? { __status: 504 } : msg.path.endsWith("/health") ? { capabilities: { expected_pane_id: true } } : { tabs: f.a.tabs };
  assert.equal((await write(f)).body.result.structuredContent.outcome_unknown, true);
  await write(f);
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
});

test("a reserved request interrupted before saving its result is never executed again", async () => {
  const f = await fixture();
  await write(f);
  const key = `mcp:${await digest(KEY)}:${await digest(args.request_id)}`;
  const entry = await f.storage.get(key);
  delete entry.result;
  await f.storage.put(key, entry);
  f.restartRegistry();
  const retry = await write(f);
  assert.equal(retry.body.result.structuredContent.code, "outcome_unknown");
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 1);
});

test("concurrent uploads keep the greatest timestamp regardless of the publishing instance", async () => {
  const f = await fixture();
  await Promise.all([
    f.configure(f.a, { [KEY]: { ...f.group, timestamp: 20, members: [A] } }),
    f.configure(f.b, { [KEY]: { ...f.group, timestamp: 30, members: [B], permissions: ["read"] } }),
  ]);
  const latest = await f.configure(f.a, { [KEY]: f.group });
  assert.equal(latest[KEY].timestamp, 30);
  assert.deepEqual(latest[KEY].members, [B]);
  assert.equal((await f.rpc("tools/list")).body.result.tools.length, 3);
});

test("open_tab chooses an explicitly authorized online instance and only launches presets", async () => {
  const f = await fixture();
  const result = await f.rpc("tools/call", { name: "open_tab", arguments: { instance_id: "inst_b", cwd: "/tmp", agent: "claude", request_id: "open_request" } });
  assert.equal(result.body.result.isError, false);
  assert.equal(f.calls.at(-1).instance, "inst_b");
  assert.equal(f.calls.at(-1).body.command, "claude");
});

test("strict schemas reject unsafe paths, arbitrary startup commands and missing input targets", async () => {
  const f = await fixture();
  for (const [name, values] of [
    ["send_message", { ...args, tab_id: "t_a/../../health" }],
    ["send_message", { ...args, instance_id: "other" }],
    ["send_message", { ...args, expected_pane_id: undefined }],
    ["open_tab", { cwd: "/tmp", instance_id: "inst_b", command: "rm -rf /", request_id: "open_request" }],
    ["send_message", { ...args, text: "x".repeat(32769) }],
  ]) {
    const result = await f.rpc("tools/call", { name, arguments: values });
    assert.ok(result.body.error || result.body.result.isError);
  }
  assert.equal(f.calls.length, 0);
});

test("official SDK client initializes, lists and calls tools through the group endpoint", async () => {
  const f = await fixture();
  const client = new Client({ name: "test-client", version: "1.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL("https://otty.example/mcp"), {
    requestInit: { headers: { Authorization: `Bearer ${KEY}` } }, fetch: (url, init) => worker.fetch(new Request(url, init), f.env),
  }));
  assert.equal((await client.listTools()).tools.length, 8);
  const result = await client.callTool({ name: "inspect_tab", arguments: { tab_id: "t_b" } });
  assert.deepEqual(result.structuredContent.screen.lines, ["inst_b"]);
  await client.close();
});

test("gateway whitelists MCP endpoints, malformed groups fail closed and stale socket pushes are ignored", async () => {
  const f = await fixture();
  const bad = await f.a.hub.fetch(internalRequest("mcp-api", { method: "POST", path: "/api/v1/asr" }));
  assert.equal(bad.status, 403);
  await assert.rejects(f.configure(f.a, { [KEY]: { timestamp: 2, permissions: ["admin"], members: [A] } }));
  assert.equal(f.a.hub.mcpConfig.enabled, false);
  const count = f.b.events.length;
  await f.b.hub.fetch(internalRequest("mcp-sync", { connection_id: "stale-session", groups: { [KEY]: f.group } }));
  assert.equal(f.b.events.length, count);
});

test("protocol errors, notifications and unsupported methods", async () => {
  const f = await fixture();
  assert.equal((await f.request("invalid json")).status, 400);
  assert.equal((await f.request({ jsonrpc: "2.0", method: "notifications/initialized" })).status, 202);
  for (const method of ["GET", "DELETE", "PUT"]) assert.equal((await f.request({}, { method })).status, 405);
  const result = await f.rpc("tools/list", {}, { headers: { "MCP-Protocol-Version": "1900-01-01" } });
  assert.equal(result.response.status, 400);
});
