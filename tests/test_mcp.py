import argparse
import asyncio
import json
import stat

import httpx
import pytest

import main
from otty_server.app import Settings, create_app
from otty_server.mcp import McpRoutes, mcp_permission
from otty_server.tunnel import InstanceCredentials, TunnelClient, build_agent_ws_url
from tests.test_api import TOKEN, FakeCLI, writes

OTHER = "another-instance-token-0123456789"
GROUP = "test-mcp-group-key-0123456789"


@pytest.fixture
def instance(tmp_path, monkeypatch):
    for key in ["OTTY_MCP_ENABLED", "OTTY_MCP_PERMISSIONS", "OTTY_MCP_ROUTES_FILE"]:
        monkeypatch.delenv(key, raising=False)
    return InstanceCredentials(
        instance_id="inst_test", token=TOKEN, path=tmp_path / "instance.json"
    )


def test_default_group_reuses_existing_token_without_generating_a_secret(instance):
    routes = McpRoutes(instance)
    assert routes.enabled
    assert routes.groups == {
        TOKEN: {
            "timestamp": 0,
            "permissions": ["read", "control", "manage"],
            "members": [TOKEN],
        }
    }
    assert routes.path.name == "mcp-groups.json"
    assert stat.S_IMODE(routes.path.stat().st_mode) == 0o600
    assert McpRoutes(instance).groups == routes.groups
    assert [p.name for p in instance.path.parent.iterdir()] == ["mcp-groups.json"]
    assert routes.registration()["version"] == 2


def test_latest_group_replaces_entire_record_and_ignores_stale_or_unsubscribed_keys(
    instance,
):
    routes = McpRoutes(instance)
    newer = {"timestamp": 10, "permissions": ["read"], "members": [TOKEN, OTHER]}
    assert routes.merge({TOKEN: newer})
    assert routes.groups[TOKEN] == newer
    assert not routes.merge({TOKEN: {**newer, "timestamp": 9, "members": []}})
    assert not routes.merge({TOKEN: {**newer, "members": []}})
    assert not routes.merge({GROUP: newer})
    assert GROUP not in routes.groups
    assert McpRoutes(instance).groups[TOKEN] == newer
    assert stat.S_IMODE(routes.path.stat().st_mode) == 0o600


def test_merge_respects_a_newer_local_edit(instance):
    routes = McpRoutes(instance)
    local = {"timestamp": 20, "permissions": ["read"], "members": []}
    routes.path.write_text(json.dumps({"groups": {TOKEN: local}}))
    assert not routes.merge({TOKEN: {**local, "timestamp": 19, "members": [TOKEN]}})
    assert routes.groups[TOKEN] == local


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_local_disable_refuses_all_mcp_operations(instance, monkeypatch, value):
    monkeypatch.setenv("OTTY_MCP_ENABLED", value)
    routes = McpRoutes(instance)
    assert not routes.enabled
    assert routes.registration()["enabled"] is False
    assert not routes.allows("GET", "/api/v1/tabs")
    assert not routes.allows("POST", "/api/v1/tabs")


def test_local_permission_ceiling_and_endpoint_whitelist(instance, monkeypatch):
    monkeypatch.setenv("OTTY_MCP_PERMISSIONS", "read")
    routes = McpRoutes(instance)
    assert routes.allows("GET", "/api/v1/tabs")
    assert routes.allows("GET", "/api/v1/tabs/t_agent/screen")
    assert not routes.allows("POST", "/api/v1/tabs/t_agent/messages")
    assert not routes.allows("DELETE", "/api/v1/tabs/t_agent")
    assert not routes.allows("GET", "/api/v1/push/status")
    assert not routes.allows("POST", "/api/v1/asr")
    for invalid in ["control", "read,admin", ""]:
        monkeypatch.setenv("OTTY_MCP_PERMISSIONS", invalid)
        with pytest.raises(ValueError):
            McpRoutes(instance)


def test_tunnel_enforces_local_permissions_even_if_gateway_forwards_a_write(
    instance, monkeypatch
):
    monkeypatch.setenv("OTTY_MCP_PERMISSIONS", "read")
    cli = FakeCLI()
    app = create_app(Settings(token=TOKEN, host_pane_id="p_host"), cli)
    tunnel = TunnelClient(
        app,
        gateway_url="https://example.test",
        register_key="test-registration-key",
        token=TOKEN,
        mcp_routes=McpRoutes(instance),
    )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            code, _ = await tunnel.execute_local(
                client,
                "POST",
                "/api/v1/tabs/t_agent/messages",
                body={"text": "hello"},
                mcp=True,
            )
            assert code == 403
            assert writes(cli) == []
            # The existing PWA/HTTP route is independent of the MCP ceiling.
            code, _ = await tunnel.execute_local(
                client, "POST", "/api/v1/tabs/t_agent/messages", body={"text": "hello"}
            )
            assert code == 200
            assert len(writes(cli)) == 1

    asyncio.run(check())


def test_tunnel_subscribes_on_reconnect_saves_pushes_and_reloads_local_routes(instance):
    routes = McpRoutes(instance)
    tunnel = TunnelClient(
        create_app(Settings(token=TOKEN, host_pane_id="p_host"), FakeCLI()),
        gateway_url="https://example.test",
        register_key="test-registration-key",
        token=TOKEN,
        mcp_routes=routes,
        push_interval=0.01,
    )

    async def check():
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        session = asyncio.create_task(tunnel.run_session(outgoing.put, incoming.get))
        first = await asyncio.wait_for(outgoing.get(), 1)
        assert first["type"] == "mcp.configure"
        newer = {"timestamp": 2, "permissions": ["read"], "members": [TOKEN, OTHER]}
        await incoming.put({"type": "mcp.groups.updated", "groups": {TOKEN: newer}})
        update = await asyncio.wait_for(outgoing.get(), 1)
        assert update["groups"][TOKEN] == newer
        assert McpRoutes(instance).groups[TOKEN] == newer
        # An on-disk edit is uploaded without restarting the tunnel.
        edited = {"timestamp": 3, "permissions": ["read"], "members": []}
        routes.path.write_text(json.dumps({"groups": {TOKEN: edited}}))
        assert (await asyncio.wait_for(outgoing.get(), 1))["groups"][TOKEN] == edited
        await incoming.put({"type": "close"})
        await session
        # Reconnect advertises the most recently synchronized local table.
        await incoming.put({"type": "close"})
        await tunnel.run_session(outgoing.put, incoming.get)
        assert (await outgoing.get())["groups"][TOKEN] == edited

    asyncio.run(check())


def test_invalid_route_file_is_rejected(instance):
    routes = McpRoutes(instance)
    for config in [
        {"timestamp": -1, "permissions": ["read"], "members": [TOKEN]},
        {"timestamp": 1, "permissions": ["admin"], "members": [TOKEN]},
        {"timestamp": 1, "permissions": ["read"], "members": ["short"]},
    ]:
        routes.path.write_text(json.dumps({"groups": {TOKEN: config}}))
        with pytest.raises(ValueError):
            McpRoutes(instance)


@pytest.mark.parametrize("enabled", [True, False])
def test_main_prints_routes_and_uses_only_existing_instance_token(
    instance, monkeypatch, capsys, enabled
):
    monkeypatch.setenv("OTTY_MCP_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("OTTY_PANE_ID", "p_host")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("OTTY_REMOTE", raising=False)
    monkeypatch.setattr(main, "load_or_create_credentials", lambda: instance)
    tunnels = []

    class FakeTunnel:
        def __init__(self, app, **kwargs):
            self.ws_url = build_agent_ws_url(kwargs["gateway_url"])
            tunnels.append(kwargs)

        async def serve_forever(self):
            pass

    monkeypatch.setattr(main, "TunnelClient", FakeTunnel)
    asyncio.run(
        main.run(
            argparse.Namespace(
                gateway="wss://example.test/sub/ws/agent",
                register_key="test-registration-key",
                listen=False,
            )
        )
    )
    output = capsys.readouterr().out
    assert len(tunnels) == 1
    assert tunnels[0]["token"] == TOKEN
    assert tunnels[0]["mcp_routes"].enabled == enabled
    if enabled:
        assert "MCP URL     : https://example.test/sub/mcp" in output
        assert "MCP Routes" in output
        assert "MCP Key" not in output
        assert "MCP Access" not in output
        assert "otty_mcp_" not in output
    else:
        assert "MCP         : disabled" in output
        assert "MCP Key" not in output


@pytest.mark.parametrize(
    "method,path,permission",
    [
        ("GET", "/api/v1/health", "read"),
        ("POST", "/api/v1/tabs", "manage"),
        ("DELETE", "/api/v1/tabs/t_agent", "manage"),
        ("POST", "/api/v1/tabs/t_agent/keys", "control"),
        ("POST", "/api/v1/tabs/t_agent/type", None),
    ],
)
def test_mcp_endpoint_permissions(method, path, permission):
    assert mcp_permission(method, path) == permission
