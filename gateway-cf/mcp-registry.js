import { z } from "zod";
import { digest, serveMcp } from "./mcp.js";

const token = z.string().min(16).max(512).refine((s) => s.trim() === s);
const permissions = z.array(z.enum(["read", "control", "manage"])).min(1).refine((p) => p.includes("read"));
export const groupsSchema = z.record(token, z.object({
  timestamp: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER),
  permissions,
  members: z.array(token).max(100),
}).strict()).refine((groups) => Object.keys(groups).length <= 100);

export function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

export function registry(env) {
  return env.OTTY_MCP.get(env.OTTY_MCP.idFromName("routes"));
}

export function internalRequest(mode, body) {
  return new Request("https://otty.internal/", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Otty-Mode": mode }, body: JSON.stringify(body),
  });
}

export function mcpPermission(method, path) {
  if (method === "GET" && path === "/api/v1/health") return "read";
  if (path === "/api/v1/tabs") return { GET: "read", POST: "manage" }[method];
  const match = /^\/api\/v1\/tabs\/t_[A-Za-z0-9_-]+(?:\/(screen|wait|messages|keys))?$/.exec(path);
  if (!match) return null;
  if (method === "GET" && [undefined, "screen", "wait"].includes(match[1])) return "read";
  if (method === "DELETE" && match[1] === undefined) return "manage";
  if (method === "POST" && ["messages", "keys"].includes(match[1])) return "control";
  return null;
}

class RouteError extends Error {
  constructor(message, status = 503) { super(message); this.status = status; }
}

class GroupRouter {
  constructor(ctx, env, group) { this.ctx = ctx; this.env = env; this.group = group; }

  async members() {
    const hashes = [...new Set(await Promise.all(this.group.members.map(digest)))];
    const results = await Promise.allSettled(hashes.map(async (hash) => {
      const stub = this.env.OTTY_HUB.get(this.env.OTTY_HUB.idFromName(hash));
      const info = await (await stub.fetch(internalRequest("mcp-info", {}))).json();
      return info.online && info.mcp_enabled ? { stub, ...info } : null;
    }));
    return results.filter((r) => r.status === "fulfilled" && r.value).map((r) => r.value);
  }

  async call(member, method, path, query = "", body = null) {
    const result = await member.stub.fetch(internalRequest("mcp-api", { method, path, query, body }));
    return { status: result.status, body: await result.json() };
  }

  async lists() {
    const members = await this.members();
    return Promise.all(members.map(async (member) => {
      try { return { member, ...await this.call(member, "GET", "/api/v1/tabs") }; }
      catch { return { member, status: 503, body: { detail: "Instance unavailable" } }; }
    }));
  }

  async listTabs() {
    const instances = {};
    const errors = [];
    for (const result of await this.lists()) {
      if (result.status === 200) instances[result.member.instance_id] = { tabs: result.body.tabs };
      else if (result.status !== 503) errors.push({ instance_id: result.member.instance_id, status: result.status });
    }
    return { instances, ...(errors.length ? { errors } : {}) };
  }

  async resolve(args) {
    let member;
    if (args.tab_id) {
      const matches = (await this.lists()).filter((r) => r.status === 200 && r.body.tabs?.some((tab) => tab.id === args.tab_id));
      if (matches.length > 1) throw new RouteError("Duplicate tab_id across group members", 409);
      member = matches[0]?.member;
    } else {
      member = (await this.members()).find((m) => m.instance_id === args.instance_id);
    }
    if (!member) throw new RouteError("Target not found among the group's online MCP instances", 404);
    return { forwardToAgent: (...params) => this.call(member, ...params) };
  }
}

export class OttyMcpRegistry {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }

  async sync(body) {
    const groups = groupsSchema.parse(body.groups);
    const hashes = await Promise.all(Object.keys(groups).map(digest));
    const latest = {};
    const changed = {};
    await this.ctx.storage.transaction(async (storage) => {
      // Transaction retries must not retain results from an earlier attempt.
      for (const key of Object.keys(latest)) delete latest[key];
      for (const key of Object.keys(changed)) delete changed[key];
      for (const [index, [key, incoming]] of Object.entries(groups).entries()) {
        const storageKey = `group:${hashes[index]}`;
        const previous = await storage.get(storageKey);
        if (!previous || incoming.timestamp > previous.config.timestamp) {
          await storage.put(storageKey, { key, config: incoming });
          latest[key] = incoming;
          changed[key] = incoming;
        } else latest[key] = previous.config;
      }
      await storage.put(`sub:${body.instance_hash}`, { connection_id: body.connection_id, keys: hashes });
    });
    if (Object.keys(changed).length) await this.broadcast(changed);
    return latest;
  }

  async broadcast(groups) {
    const hashes = new Map(await Promise.all(Object.keys(groups).map(async (key) => [key, await digest(key)])));
    const subscribers = await this.ctx.storage.list({ prefix: "sub:" });
    await Promise.allSettled([...subscribers].map(async ([key, sub]) => {
      const updates = Object.fromEntries(Object.entries(groups).filter(([token]) => sub.keys.includes(hashes.get(token))));
      if (!Object.keys(updates).length) return;
      const stub = this.env.OTTY_HUB.get(this.env.OTTY_HUB.idFromName(key.slice(4)));
      await stub.fetch(internalRequest("mcp-sync", { connection_id: sub.connection_id, groups: updates }));
    }));
  }

  async fetch(request) {
    const mode = request.headers.get("X-Otty-Mode");
    if (mode === "mcp-publish") {
      try { return json({ groups: await this.sync(await request.json()) }); }
      catch { return json({ detail: "Invalid MCP group routes" }, 400); }
    }
    if (mode === "mcp-unsubscribe") {
      const body = await request.json();
      await this.ctx.storage.transaction(async (storage) => {
        const key = `sub:${body.instance_hash}`;
        if ((await storage.get(key))?.connection_id === body.connection_id) await storage.delete(key);
      });
      return json({ ok: true });
    }
    if (mode !== "mcp") return json({ detail: "Not Found" }, 404);
    const keyHash = request.headers.get("X-Otty-Mcp-Key");
    const group = keyHash && await this.ctx.storage.get(`group:${keyHash}`);
    if (!group) return json({ error: "Invalid or missing MCP group key" }, 401, { "WWW-Authenticate": 'Bearer realm="otty-mcp"' });
    if (request.method !== "POST") return json({ error: "Stateless MCP supports POST only" }, 405, { Allow: "POST, OPTIONS" });
    const router = new GroupRouter(this.ctx, this.env, group.config);
    return serveMcp(request, router, { client_hash: keyHash, permissions: group.config.permissions });
  }
}
