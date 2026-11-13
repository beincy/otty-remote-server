"""Gateway-side MCP: group routing, permissions, sync and idempotent writes.

Mirrors the behaviour locked down by ``gateway-cf/tests/mcp.test.js`` and runs
it against the real FastAPI gateway with scripted agent tunnels on /ws/agent.
"""

import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.app import GatewaySettings, create_gateway_app
from gateway.mcp_registry import McpRegistry, mcp_permission, validate_groups

REGISTER_KEY = "gateway-register-secret-key"
KEY = "mcp-group-key-000000000001"
TOKEN_A = "instance-token-A-000000000001"
TOKEN_B = "instance-token-B-000000000001"
TOKEN_C = "offline-instance-token-00000001"
ALL = ["read", "control", "manage"]

ACCEPT = {"Accept": "application/json", "Content-Type": "application/json"}


def group(timestamp=1, members=(TOKEN_A, TOKEN_B, TOKEN_C), permissions=ALL):
    return {
        "timestamp": timestamp,
        "permissions": permissions,
        "members": list(members),
    }


class FakeAgent:
    """A scripted local Otty Server connected to the gateway's agent tunnel."""

    def __init__(self, gw_client, token, instance_id, tabs, responder=None):
        self.instance_id = instance_id
        self.tabs = tabs
        self.responder = responder
        self.requests: list[dict] = []
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._ws_cm = gw_client.websocket_connect(
            "/ws/agent",
            headers={
                "X-Register-Key": REGISTER_KEY,
                "Authorization": f"Bearer {token}",
                "X-Instance-Id": instance_id,
            },
        )
        self._ws = self._ws_cm.__enter__()
        registered = self._ws.receive_json()
        assert registered["type"] == "registered"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _respond(self, msg: dict):
        if self.responder is not None:
            reply = self.responder(msg)
            if reply is not None:
                return reply
        method, path = msg.get("method", "GET"), msg.get("path", "")
        if path == "/api/v1/health":
            return 200, {"status": "ok", "capabilities": {"expected_pane_id": True}}
        if path == "/api/v1/tabs":
            return 200, {"tabs": self.tabs}
        if path.endswith("/screen"):
            return 200, {
                "lines": [self.instance_id],
                "hasMore": False,
                "nextBefore": None,
            }
        if path.endswith("/wait"):
            return 200, {"done": True, "tab": self.tabs[0] if self.tabs else {}}
        if method in ("POST", "DELETE"):
            target = (
                self.tabs[0] if self.tabs else {"id": "t_none", "pane_id": "p_none"}
            )
            return 200, {
                "tab_id": target["id"],
                "pane_id": target["pane_id"],
                "receipt": {"queued": True},
            }
        return 200, next((t for t in self.tabs if path.endswith("/" + t["id"])), {})

    def _serve(self):
        while not self._closed.is_set():
            try:
                msg = self._ws.receive_json()
            except WebSocketDisconnect, RuntimeError, OSError, anyio.EndOfStream:
                break
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "request":
                self.requests.append(msg)
                status, body = self._respond(msg)
                with self._lock:
                    self._ws.send_json(
                        {
                            "type": "response",
                            "id": msg.get("id"),
                            "status": status,
                            "body": body,
                        }
                    )
            else:
                self.events.append(msg)

    def configure(self, gw_client, groups, *, enabled=True, permissions=ALL):
        self._ws.send_json(
            {
                "type": "mcp.configure",
                "version": 2,
                "enabled": enabled,
                "permissions": permissions,
                "groups": groups,
            }
        )
        return self.wait_for_event(lambda e: e.get("type") == "mcp.configured")

    def wait_for_event(self, predicate, timeout=5.0, since=None):
        start = len(self.events) if since is None else since
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events[start:]:
                if predicate(event):
                    return event
            time.sleep(0.01)
        raise AssertionError("Expected gateway event did not arrive")

    def close(self):
        self._closed.set()
        with suppress(Exception):
            self._ws_cm.__exit__(None, None, None)
        self._thread.join(timeout=1)


@pytest.fixture()
def gw_client(tmp_path):
    settings = GatewaySettings(
        register_key=REGISTER_KEY, mcp_store=tmp_path / "gateway-mcp.json"
    )
    with TestClient(create_gateway_app(settings)) as client:
        yield client


def rpc(gw_client, method, params=None, *, token=KEY, headers=None, body=None):
    payload = (
        body
        if body is not None
        else {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    return gw_client.post(
        "/mcp",
        headers={**ACCEPT, "Authorization": f"Bearer {token}", **(headers or {})},
        json=payload,
    )


def result_of(res):
    return res.json()["result"]


def test_list_tabs_groups_online_members_and_hides_offline_and_tokens(gw_client):
    a = FakeAgent(
        gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a", "cwd": "/a"}]
    )
    b = FakeAgent(
        gw_client, TOKEN_B, "inst_b", [{"id": "t_b", "pane_id": "p_b", "cwd": "/b"}]
    )
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        structured = result_of(rpc(gw_client, "tools/call", {"name": "list_tabs"}))[
            "structuredContent"
        ]
        assert list(structured["instances"]) == ["inst_a", "inst_b"]
        assert structured["instances"]["inst_b"]["tabs"][0]["id"] == "t_b"
        raw = json.dumps(structured)
        assert TOKEN_A not in raw and TOKEN_B not in raw and TOKEN_C not in raw
        # Every forwarded call is marked as MCP so the local ceiling applies.
        assert a.requests and b.requests
        assert all(req["source"] == "mcp" for req in a.requests + b.requests)
    finally:
        a.close()
        b.close()


def test_inspect_and_wait_read_tools(gw_client):
    a = FakeAgent(
        gw_client,
        TOKEN_A,
        "inst_a",
        [{"id": "t_a", "pane_id": "p_a", "agent_state": "idle"}],
    )
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        inspect = result_of(
            rpc(
                gw_client,
                "tools/call",
                {"name": "inspect_tab", "arguments": {"tab_id": "t_a"}},
            )
        )
        assert inspect["isError"] is False
        assert inspect["structuredContent"]["screen"]["lines"] == ["inst_a"]
        assert inspect["structuredContent"]["tab"]["id"] == "t_a"
        wait = result_of(
            rpc(
                gw_client,
                "tools/call",
                {"name": "wait_tab", "arguments": {"tab_id": "t_a"}},
            )
        )
        assert wait["structuredContent"]["status"] == "idle"
    finally:
        a.close()
        b.close()


def test_group_membership_limits_tab_lookup(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [{"id": "t_b", "pane_id": "p_b"}])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        since = len(a.events)
        b.configure(gw_client, {KEY: group(timestamp=2, members=[TOKEN_B])})
        a.wait_for_event(lambda e: e.get("type") == "mcp.groups.updated", since=since)
        denied = result_of(
            rpc(
                gw_client,
                "tools/call",
                {"name": "inspect_tab", "arguments": {"tab_id": "t_a"}},
            )
        )
        assert denied["isError"] is True
        assert denied["structuredContent"]["http_status"] == 404
        assert not a.requests
    finally:
        a.close()
        b.close()


def test_permissions_hide_tools_and_block_invocation(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group(permissions=["read"])})
        b.configure(gw_client, {KEY: group(permissions=["read"])})
        tools = result_of(rpc(gw_client, "tools/list"))["tools"]
        assert [t["name"] for t in tools] == ["list_tabs", "inspect_tab", "wait_tab"]
        denied = rpc(
            gw_client,
            "tools/call",
            {
                "name": "send_message",
                "arguments": {
                    "tab_id": "t_a",
                    "expected_pane_id": "p_a",
                    "text": "hi",
                    "request_id": "req_perm_01",
                },
            },
        )
        body = denied.json()
        assert body.get("error") or body["result"]["isError"]
        assert not a.requests and not b.requests
    finally:
        a.close()
        b.close()


def test_local_ceiling_blocks_writes_but_pwa_still_works(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()}, permissions=["read"])
        b.configure(gw_client, {KEY: group()})
        denied = result_of(
            rpc(
                gw_client,
                "tools/call",
                {
                    "name": "send_message",
                    "arguments": {
                        "tab_id": "t_a",
                        "expected_pane_id": "p_a",
                        "text": "hi",
                        "request_id": "req_ceiling_1",
                    },
                },
            )
        )
        assert denied["structuredContent"]["http_status"] == 403
        # Resolve lookups may read, but the write itself never reaches the host.
        assert not any(r["method"] == "POST" for r in a.requests)
        # The ordinary instance token keeps full PWA access, forwarded without
        # the MCP source marker.
        pwa = gw_client.get(
            "/api/v1/tabs", headers={"Authorization": f"Bearer {TOKEN_A}"}
        )
        assert pwa.status_code == 200
        assert all("source" not in r for r in a.requests[len(a.requests) - 1 :])
    finally:
        a.close()
        b.close()


def test_disabled_instance_is_skipped_without_affecting_others(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [{"id": "t_b", "pane_id": "p_b"}])
    try:
        a.configure(gw_client, {KEY: group()}, enabled=False)
        b.configure(gw_client, {KEY: group()})
        structured = result_of(rpc(gw_client, "tools/call", {"name": "list_tabs"}))[
            "structuredContent"
        ]
        assert list(structured["instances"]) == ["inst_b"]
    finally:
        a.close()
        b.close()


def test_newest_timestamp_wins_stale_and_equal_lose(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [{"id": "t_b", "pane_id": "p_b"}])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})

        def pushes():
            return [e for e in a.events if e.get("type") == "mcp.groups.updated"]

        events_before = len(pushes())
        newer = group(timestamp=10, members=[TOKEN_B], permissions=["read"])
        b.configure(gw_client, {KEY: newer})
        pushed = a.wait_for_event(
            lambda e: e.get("type") == "mcp.groups.updated", since=events_before
        )
        assert pushed["groups"][KEY] == newer
        events_after_push = len(pushes())
        # Stale upload loses and converges on the stored version:
        reply = a.configure(gw_client, {KEY: group()})
        assert reply["groups"][KEY] == newer
        # Equal timestamp is ignored too:
        reply = a.configure(gw_client, {KEY: group(timestamp=10)})
        assert reply["groups"][KEY] == newer
        assert len(pushes()) == events_after_push
        tools = result_of(rpc(gw_client, "tools/list"))["tools"]
        assert len(tools) == 3
        structured = result_of(rpc(gw_client, "tools/call", {"name": "list_tabs"}))[
            "structuredContent"
        ]
        assert list(structured["instances"]) == ["inst_b"]
    finally:
        a.close()
        b.close()


def test_unsubscribed_key_is_not_pushed_and_cleared_members_survive(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [{"id": "t_b", "pane_id": "p_b"}])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        a.configure(gw_client, {})  # unsubscribe from every key
        events_before = len(a.events)
        b.configure(gw_client, {KEY: group(timestamp=5, members=[])})
        assert len(a.events) == events_before
        structured = result_of(rpc(gw_client, "tools/call", {"name": "list_tabs"}))[
            "structuredContent"
        ]
        assert structured["instances"] == {}
        # A stale resubscription does not resurrect the cleared members.
        a.configure(gw_client, {KEY: group()})
        structured = result_of(rpc(gw_client, "tools/call", {"name": "list_tabs"}))[
            "structuredContent"
        ]
        assert structured["instances"] == {}
    finally:
        a.close()
        b.close()


def test_write_deduplicates_and_conflicting_reuse_never_executes(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        call = {
            "name": "send_message",
            "arguments": {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "text": "hello",
                "request_id": "request_123",
            },
        }
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: rpc(gw_client, "tools/call", call).json(), range(4))
            )
        assert any(r["result"]["isError"] is False for r in results)
        assert len([r for r in a.requests if r["method"] == "POST"]) == 1
        conflict = rpc(
            gw_client,
            "tools/call",
            {**call, "arguments": {**call["arguments"], "text": "changed"}},
        ).json()
        assert conflict["result"]["structuredContent"]["code"] == "request_id_conflict"
    finally:
        a.close()
        b.close()


def test_uncertain_failure_is_replayed_without_repeating_the_write(gw_client):
    def flaky(msg):
        if msg["path"].endswith("/messages"):
            return 504, {"detail": "Timed out waiting for local Otty server response"}
        return None

    a = FakeAgent(
        gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}], responder=flaky
    )
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        call = {
            "name": "send_message",
            "arguments": {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "text": "hello",
                "request_id": "request_456",
            },
        }
        first = result_of(rpc(gw_client, "tools/call", call))
        assert first["structuredContent"]["outcome_unknown"] is True
        # Retry with identical arguments replays the uncertain receipt.
        result_of(rpc(gw_client, "tools/call", call))
        assert len([r for r in a.requests if r["method"] == "POST"]) == 1
    finally:
        a.close()
        b.close()


def test_registry_persistence_survives_gateway_restart(tmp_path, gw_client):
    store = tmp_path / "gateway-mcp.json"
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        call = {
            "name": "send_message",
            "arguments": {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "text": "hello",
                "request_id": "request_789",
            },
        }
        first = rpc(gw_client, "tools/call", call).json()
        assert first["result"]["isError"] is False
    finally:
        a.close()
        b.close()

    # A brand-new gateway process loads groups, subscriptions and receipts.
    with TestClient(
        create_gateway_app(GatewaySettings(register_key=REGISTER_KEY, mcp_store=store))
    ) as client:
        a = FakeAgent(client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
        b = FakeAgent(client, TOKEN_B, "inst_b", [])
        try:
            a.configure(client, {KEY: group()})
            b.configure(client, {KEY: group()})
            replayed = rpc(client, "tools/call", call).json()
            assert replayed == first
            assert len([r for r in a.requests if r["method"] == "POST"]) == 0
            # Subscriptions persisted as well: a new upload still pushes updates.
            since = len(a.events)
            newer = group(timestamp=7, members=[TOKEN_A])
            reply = b.configure(client, {KEY: newer})
            assert reply["groups"][KEY] == newer
            pushed = a.wait_for_event(
                lambda e: e.get("type") == "mcp.groups.updated", since=since
            )
            assert pushed["groups"][KEY]["timestamp"] == 7
        finally:
            a.close()
            b.close()


def test_open_tab_targets_explicit_instance_with_presets_only(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        res = rpc(
            gw_client,
            "tools/call",
            {
                "name": "open_tab",
                "arguments": {
                    "instance_id": "inst_b",
                    "cwd": "/tmp",
                    "agent": "claude",
                    "request_id": "open_request",
                },
            },
        )
        assert result_of(res)["isError"] is False
        forwarded = [r for r in b.requests if r["method"] == "POST"][-1]
        assert forwarded["body"]["command"] == "claude"
        assert not any(r["method"] == "POST" for r in a.requests)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "send_message",
            {
                "tab_id": "t_a/../../health",
                "expected_pane_id": "p_a",
                "text": "hi",
                "request_id": "request_001",
            },
        ),
        (
            "send_message",
            {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "text": "hi",
                "request_id": "request_002",
                "extra": 1,
            },
        ),
        ("send_message", {"tab_id": "t_a", "text": "hi", "request_id": "request_003"}),
        (
            "send_message",
            {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "text": "x" * 32769,
                "request_id": "request_004",
            },
        ),
        (
            "open_tab",
            {
                "instance_id": "inst_a",
                "cwd": "relative/path",
                "request_id": "request_005",
            },
        ),
        (
            "open_tab",
            {
                "instance_id": "inst_a",
                "cwd": "/tmp",
                "command": "rm -rf /",
                "request_id": "request_006",
            },
        ),
        (
            "send_keys",
            {
                "tab_id": "t_a",
                "expected_pane_id": "p_a",
                "keys": ["Format"],
                "request_id": "request_007",
            },
        ),
    ],
)
def test_strict_schemas_reject_unsafe_arguments(gw_client, name, arguments):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    b = FakeAgent(gw_client, TOKEN_B, "inst_b", [])
    try:
        a.configure(gw_client, {KEY: group()})
        b.configure(gw_client, {KEY: group()})
        res = rpc(
            gw_client, "tools/call", {"name": name, "arguments": arguments}
        ).json()
        assert res.get("error") or res["result"]["isError"]
        assert not a.requests and not b.requests
    finally:
        a.close()
        b.close()


def test_origin_policy_is_independent_of_pwa_cors(tmp_path):
    settings = GatewaySettings(
        register_key=REGISTER_KEY,
        cors_origins=("*",),
        mcp_allowed_origins=("https://client.test",),
        mcp_store=tmp_path / "gateway-mcp.json",
    )
    with TestClient(create_gateway_app(settings)) as client:
        evil = {"Origin": "https://evil.test"}
        assert (
            client.post("/mcp", headers={**ACCEPT, **evil}, json={}).status_code == 403
        )
        assert client.options("/mcp", headers=evil).status_code == 403
        preflight = client.options("/mcp", headers={"Origin": "https://client.test"})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "https://client.test"
        # No Origin (server-to-server clients) is allowed:
        assert (
            client.post("/mcp", headers=ACCEPT, json={}).status_code == 401
        )  # auth, not origin


def test_protocol_errors_and_unsupported_methods(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [])
    try:
        a.configure(gw_client, {KEY: group()})
        # Not JSON at all -> parse error:
        raw = gw_client.post(
            "/mcp",
            headers={**ACCEPT, "Authorization": f"Bearer {KEY}"},
            content="invalid json",
        )
        assert raw.status_code == 400
        # Valid JSON but not a JSON-RPC object -> invalid request:
        res = gw_client.post(
            "/mcp",
            headers={**ACCEPT, "Authorization": f"Bearer {KEY}"},
            content='"a string"',
        )
        assert res.status_code == 400
        res = gw_client.post(
            "/mcp",
            headers={**ACCEPT, "Authorization": f"Bearer {KEY}"},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert res.status_code == 202
        for method in ("GET", "DELETE", "PUT"):
            res = gw_client.request(
                method, "/mcp", headers={"Authorization": f"Bearer {KEY}"}
            )
            assert res.status_code == 405
        assert (
            rpc(
                gw_client, "tools/list", headers={"MCP-Protocol-Version": "1900-01-01"}
            ).status_code
            == 400
        )
    finally:
        a.close()


def test_unknown_group_keys_url_tokens_and_forged_headers_cannot_authenticate(
    gw_client,
):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [{"id": "t_a", "pane_id": "p_a"}])
    try:
        a.configure(gw_client, {KEY: group()})
        forged = {
            "X-Otty-Mode": "mcp-publish",
            "X-Otty-Mcp-Key": KEY,
            "X-Otty-Mcp-Policy": '{"permissions":["manage"]}',
        }
        for token in (None, "unknown-token-000000000001"):
            for method in ("POST", "GET", "DELETE"):
                res = gw_client.request(
                    method,
                    f"/mcp?token={KEY}",
                    headers={
                        **ACCEPT,
                        "Content-Type": "application/json",
                        **forged,
                        **(
                            {}
                            if token is None
                            else {"Authorization": f"Bearer {token}"}
                        ),
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert res.status_code == 401, (method, token, res.status_code)
        assert not a.requests
    finally:
        a.close()


def test_official_shape_initialize_and_ping(gw_client):
    a = FakeAgent(gw_client, TOKEN_A, "inst_a", [])
    try:
        a.configure(gw_client, {KEY: group()})
        init = result_of(
            rpc(
                gw_client,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            )
        )
        assert init["protocolVersion"] == "2025-06-18"
        assert init["serverInfo"]["name"] == "otty"
        assert "instructions" in init
        assert len(result_of(rpc(gw_client, "tools/list"))["tools"]) == 8
        assert result_of(rpc(gw_client, "ping")) == {}
        assert rpc(gw_client, "does/not/exist").json()["error"]["code"] == -32601
    finally:
        a.close()


# --- unit-level checks -------------------------------------------------------


def test_mcp_permission_mapping():
    assert mcp_permission("GET", "/api/v1/health") == "read"
    assert mcp_permission("GET", "/api/v1/tabs") == "read"
    assert mcp_permission("POST", "/api/v1/tabs") == "manage"
    assert mcp_permission("GET", "/api/v1/tabs/t_a1/screen") == "read"
    assert mcp_permission("GET", "/api/v1/tabs/t_a1/wait") == "read"
    assert mcp_permission("POST", "/api/v1/tabs/t_a1/messages") == "control"
    assert mcp_permission("POST", "/api/v1/tabs/t_a1/keys") == "control"
    assert mcp_permission("DELETE", "/api/v1/tabs/t_a1") == "manage"
    assert mcp_permission("GET", "/api/v1/tabs/t_a1") == "read"
    for denied in [
        ("POST", "/api/v1/asr"),
        ("POST", "/api/v1/tabs/t_a1/screen"),
        ("GET", "/api/v1/push/status"),
        ("POST", "/api/v1/tabs/bad_id/keys"),
    ]:
        assert mcp_permission(*denied) is None


def test_validate_groups_rejects_unsafe_uploads():
    assert validate_groups({KEY: group()}) == {KEY: group()}
    for bad in [
        {KEY: {**group(), "timestamp": -1}},
        {KEY: {**group(), "timestamp": "1"}},
        {KEY: {**group(), "permissions": ["admin"]}},
        {KEY: {**group(), "permissions": []}},
        {KEY: {**group(), "members": "x"}},
        {KEY: {**group(), "members": ["short"]}},
        {"short": group()},
        {f"{'x' * 513}": group()},
        {"a b": group()},
    ]:
        with pytest.raises((ValueError, TypeError)):
            validate_groups(bad)


def test_registry_persists_and_merges_by_timestamp(tmp_path):
    store = tmp_path / "registry.json"

    async def noop(instance_hash, connection_id, updates):
        return None

    first = McpRegistry(store)
    latest = asyncio.run(
        first.sync("inst-hash", "conn-1", {KEY: group(timestamp=5)}, noop)
    )
    assert latest[KEY]["timestamp"] == 5
    # Older upload from another instance loses; newest returns latest state.
    second = McpRegistry(store)
    latest = asyncio.run(
        second.sync("other-hash", "conn-2", {KEY: group(timestamp=2)}, noop)
    )
    assert latest[KEY]["timestamp"] == 5
    key_hash = hashlib.sha256(KEY.encode()).hexdigest()
    assert second.group(key_hash)["timestamp"] == 5
    # Subscriptions persist and unsubscribe is connection-guarded.
    assert store.exists()
    asyncio.run(second.unsubscribe("inst-hash", "wrong-conn"))
    assert "inst-hash" in second.subs
    asyncio.run(second.unsubscribe("inst-hash", "conn-1"))
    assert "inst-hash" not in second.subs
