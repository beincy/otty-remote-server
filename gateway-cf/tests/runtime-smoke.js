// Real Worker + SQLite DOs, simulated local Otty instances; disposable credentials only.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const KEY = "runtime-mcp-group-key-0123456789";
const A = "runtime-instance-A-0123456789";
const B = "runtime-instance-B-0123456789";
const C = "runtime-offline-instance-C-0123456789";
const ALL = ["read", "control", "manage"];
const firstGroup = { timestamp: 1, permissions: ALL, members: [A, B, C] };
const portServer = createServer();
await new Promise((resolve) => portServer.listen(0, "127.0.0.1", resolve));
const port = portServer.address().port;
await new Promise((resolve) => portServer.close(resolve));
const dir = await mkdtemp(join(tmpdir(), "otty-mcp-groups-"));
const base = `http://127.0.0.1:${port}`;
let child, exited, client;
let agents = [];
let logs = "";
function deadline(promise, ms = 10000) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("Runtime operation timed out")), ms); })]).finally(() => clearTimeout(timer));
}
async function start() {
  child = spawn(process.execPath, ["node_modules/wrangler/bin/wrangler.js", "dev", "--local", "--ip", "127.0.0.1", "--port", String(port), "--persist-to", dir,
    "--var", "OTTY_GATEWAY_REGISTER_KEY:runtime-registration-secret-0123456789",
  ], { cwd: new URL("..", import.meta.url), env: { ...process.env, WRANGLER_SEND_METRICS: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  for (const stream of [child.stdout, child.stderr]) stream.on("data", (data) => { logs = (logs + data).slice(-8000); });
  exited = new Promise((resolve) => child.once("exit", resolve));
  for (let i = 0; i < 150; i++) {
    try { if ((await fetch(`${base}/gateway/health`, { signal: AbortSignal.timeout(500) })).ok) return; } catch {}
    if (child.exitCode !== null) throw new Error("Wrangler exited before becoming ready");
    await delay(200);
  }
  throw new Error("Worker startup timed out");
}
async function stop() {
  await client?.close(); client = null;
  for (const agent of agents) agent.socket.close();
  agents = [];
  if (!child) return;
  child.kill("SIGTERM");
  const timer = setTimeout(() => child.kill("SIGKILL"), 3000);
  await exited;
  clearTimeout(timer);
  child = null;
}
async function connectClient() {
  client = new Client({ name: "runtime-group-smoke", version: "1.0" });
  await deadline(client.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${KEY}` } },
  })));
}
async function agent(token, id, group = firstGroup, permissions = ALL) {
  const socket = new WebSocket(`ws://127.0.0.1:${port}/ws/agent?${new URLSearchParams({ token, register_key: "runtime-registration-secret-0123456789", instance_id: id })}`);
  const state = { socket, writes: 0, updates: [], configured: [] };
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });
  const config = (groups, extra = {}) => ({ type: "mcp.configure", version: 2, enabled: true, permissions, groups, ...extra });
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "registered") socket.send(JSON.stringify(config({ [KEY]: group })));
    if (msg.type === "mcp.groups.updated") state.updates.push(msg.groups);
    if (msg.type === "mcp.configured") { state.configured.push(msg); resolveReady(msg); }
    if (msg.type !== "request") return;
    let body;
    const tab = { id: `t_${id}`, pane_id: `p_${id}`, agent_state: "idle", agent: "codex" };
    if (msg.path === "/api/v1/health") body = { capabilities: { expected_pane_id: true } };
    else if (msg.path === "/api/v1/tabs") body = { tabs: [tab] };
    else if (msg.path.endsWith("/messages")) {
      assert.equal(msg.source, "mcp");
      assert.equal(msg.body.expected_pane_id, `p_${id}`);
      state.writes++;
      body = { tab_id: tab.id, pane_id: tab.pane_id, receipt: { queued: true } };
    } else body = tab;
    socket.send(JSON.stringify({ type: "response", id: msg.id, status: 200, body }));
  });
  state.configure = async (groups, extra = {}) => {
    let listener;
    const done = new Promise((resolve) => {
      listener = (event) => { const msg = JSON.parse(event.data); if (msg.type === "mcp.configured") resolve(msg); };
      socket.addEventListener("message", listener);
    });
    socket.send(JSON.stringify(config(groups, extra)));
    try { return await deadline(done); } finally { socket.removeEventListener("message", listener); }
  };
  agents.push(state);
  await deadline(ready);
  return state;
}
const call = (name, args = {}) => deadline(client.callTool({ name, arguments: args }));
try {
  await start();
  assert.equal((await fetch(`${base}/mcp`, { method: "POST", body: "{}" })).status, 401);
  const a = await agent(A, "inst_a");
  const b = await agent(B, "inst_b", { ...firstGroup, timestamp: 2 });
  assert.equal(a.updates.at(-1)[KEY].timestamp, 2);
  await connectClient();
  assert.equal((await client.listTools()).tools.length, 8);
  assert.deepEqual(Object.keys((await call("list_tabs")).structuredContent.instances), ["inst_a", "inst_b"]);
  const args = { tab_id: "t_inst_b", expected_pane_id: "p_inst_b", text: "test", request_id: "runtime_write_1" };
  const receipt = await call("send_message", args);
  assert.equal(receipt.isError, false, JSON.stringify(receipt));
  assert.deepEqual(await call("send_message", args), receipt);
  assert.equal(b.writes, 1);
  assert.equal(a.writes, 0);
  const readOnly = { timestamp: 3, permissions: ["read"], members: [B] };
  await b.configure({ [KEY]: readOnly });
  assert.equal((await client.listTools()).tools.length, 3);
  assert.deepEqual(Object.keys((await call("list_tabs")).structuredContent.instances), ["inst_b"]);
  assert.equal((await a.configure({ [KEY]: firstGroup })).groups[KEY].timestamp, 3);
  const latest = { ...firstGroup, timestamp: 4 };
  await b.configure({ [KEY]: latest }, { permissions: ["read"] });
  const denied = await call("send_message", { ...args, request_id: "runtime_write_2" });
  assert.equal(denied.structuredContent.http_status, 403);
  assert.equal(b.writes, 1);
  await b.configure({ [KEY]: latest }, { enabled: false });
  assert.deepEqual(Object.keys((await call("list_tabs")).structuredContent.instances), ["inst_a"]);
  // Restart everything except SQLite. Group routes must exist before any upload.
  await stop();
  await start();
  await connectClient();
  assert.equal((await client.listTools()).tools.length, 8);
  assert.deepEqual((await call("list_tabs")).structuredContent.instances, {});
  const reconnectedA = await agent(A, "inst_a", firstGroup);
  const reconnectedB = await agent(B, "inst_b", firstGroup);
  assert.equal(reconnectedA.configured.at(-1).groups[KEY].timestamp, 4);
  assert.equal(reconnectedB.configured.at(-1).groups[KEY].timestamp, 4);
  assert.deepEqual(Object.keys((await call("list_tabs")).structuredContent.instances), ["inst_a", "inst_b"]);
  assert.deepEqual(await call("send_message", args), receipt);
  assert.equal(reconnectedB.writes, 0);
  console.log("Runtime smoke passed: multi-instance groups, LWW sync/push, permission ceilings, offline filtering, SQLite restart recovery and durable write deduplication.");
} catch (error) {
  console.error(logs);
  throw error;
} finally {
  await stop();
  await rm(dir, { recursive: true, force: true });
}
