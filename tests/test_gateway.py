import asyncio
import stat
import threading
from contextlib import contextmanager, suppress

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from otty_server.app import Settings, create_app
from otty_server.gateway import GatewaySettings, create_gateway_app, default_instance_id
from otty_server.tunnel import (
    TunnelClient,
    build_agent_ws_url,
    load_or_create_credentials,
)
from tests.test_api import TOKEN, FakeCLI

REGISTER_KEY = "gateway-register-secret-key"
INSTANCE_ID = "inst_macbook01"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@contextmanager
def connected_tunnel(
    gw_client: TestClient,
    local_app,
    *,
    token: str = TOKEN,
    instance_id: str = INSTANCE_ID,
    register_key: str = REGISTER_KEY,
    push_interval: float = 0.05,
):
    """Run TunnelClient in a background event loop over TestClient's /ws/agent."""
    tunnel = TunnelClient(
        local_app,
        gateway_url="https://gw.example.com",
        register_key=register_key,
        token=token,
        instance_id=instance_id,
        push_interval=push_interval,
    )
    with gw_client.websocket_connect("/ws/agent", headers=tunnel.headers()) as ws:
        reg = ws.receive_json()
        assert reg["type"] == "registered"
        assert reg["instance_id"] == instance_id

        send_lock = threading.Lock()
        closed = threading.Event()

        async def send_json(payload):
            if closed.is_set():
                return
            with send_lock:
                await asyncio.to_thread(ws.send_json, payload)

        async def recv_json():
            return await asyncio.to_thread(ws.receive_json)

        def runner():
            loop = asyncio.new_event_loop()
            with suppress(
                OSError, RuntimeError, WebSocketDisconnect, anyio.EndOfStream
            ):
                loop.run_until_complete(tunnel.run_session(send_json, recv_json))
            loop.close()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            yield tunnel
        finally:
            closed.set()
            ws.close()
            thread.join(timeout=1)


def test_credentials_auto_create_and_reload(tmp_path, monkeypatch):
    monkeypatch.delenv("OTTY_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("OTTY_INSTANCE_ID", raising=False)
    cred_file = tmp_path / "otty-server" / "instance.json"

    first = load_or_create_credentials(path=cred_file)
    assert first.created is True
    assert first.token.startswith("otty_") and len(first.token) >= 32
    assert first.instance_id == default_instance_id(first.token)
    mode = stat.S_IMODE(cred_file.stat().st_mode)
    assert mode == 0o600

    second = load_or_create_credentials(path=cred_file)
    assert second.created is False
    assert (second.instance_id, second.token) == (first.instance_id, first.token)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://gw.example.com", "wss://gw.example.com/ws/agent"),
        ("http://127.0.0.1:8080/", "ws://127.0.0.1:8080/ws/agent"),
        ("wss://gw.example.com/sub/ws/agent", "wss://gw.example.com/sub/ws/agent"),
    ],
)
def test_build_agent_ws_url(raw, expected):
    assert build_agent_ws_url(raw) == expected


def test_gateway_rejects_bad_register_key():
    gw_app = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY))
    with TestClient(gw_app) as client:
        for headers in [
            {},
            {"X-Register-Key": "wrong-key", "Authorization": f"Bearer {TOKEN}"},
            {"X-Register-Key": REGISTER_KEY, "Authorization": "Bearer short"},
        ]:
            with (
                pytest.raises(WebSocketDisconnect) as exc,
                client.websocket_connect("/ws/agent", headers=headers),
            ):
                pass
            assert exc.value.code == 4401


def test_http_forwarding_by_token_and_by_instance():
    cli = FakeCLI()
    local_app = create_app(Settings(token=TOKEN, host_pane_id="p_host"), cli)
    gw_app = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY))

    with TestClient(gw_app) as gw_client:
        # Offline before local server connects:
        assert (
            gw_client.get(f"/i/{INSTANCE_ID}/api/v1/tabs", headers=AUTH).status_code
            == 503
        )
        assert gw_client.get("/api/v1/tabs", headers=AUTH).status_code == 401

        with connected_tunnel(gw_client, local_app):
            assert gw_client.get("/gateway/health").json()["online_instances"] == 1

            # 1. Direct /api/v1/... routed by Bearer token:
            res = gw_client.get("/api/v1/tabs", headers=AUTH)
            assert res.status_code == 200
            assert [t["id"] for t in res.json()["tabs"]] == [
                "t_host",
                "t_agent",
                "t_shell",
            ]

            # 2. Instance-prefixed /i/{instance_id}/api/v1/...:
            res = gw_client.post(
                f"/i/{INSTANCE_ID}/api/v1/tabs/t_agent/messages",
                headers=AUTH,
                json={"text": "通过 gateway 发送"},
            )
            assert res.status_code == 200
            assert res.json()["receipt"] == {"queued": True, "pending": 1}
            assert cli.calls[-1][1] == "通过 gateway 发送"

            # 3. Screen pagination with query params:
            screen = gw_client.get(
                f"/i/{INSTANCE_ID}/api/v1/tabs/t_agent/screen",
                headers=AUTH,
                params={"limit": 15},
            ).json()
            assert len(screen["lines"]) == 15
            assert screen["hasMore"] is True

            # 4. Keys and Wait:
            keys_res = gw_client.post(
                "/api/v1/tabs/t_agent/keys",
                headers=AUTH,
                json={"keys": ["Down", "Enter"]},
            )
            assert keys_res.status_code == 200
            wait_res = gw_client.get(
                "/api/v1/tabs/t_agent/wait", headers=AUTH, params={"timeout": 2}
            )
            assert wait_res.status_code == 200
            assert wait_res.json()["done"] is True

            # 5. Auth failures:
            assert (
                gw_client.get(
                    f"/i/{INSTANCE_ID}/api/v1/tabs",
                    headers={"Authorization": "Bearer wrong-token-00000000"},
                ).status_code
                == 401
            )
            assert gw_client.get("/api/v1/tabs").status_code == 401

        # Back offline after tunnel closes:
        assert (
            gw_client.get(f"/i/{INSTANCE_ID}/api/v1/tabs", headers=AUTH).status_code
            == 503
        )


def test_client_websocket_push_and_rpc():
    cli = FakeCLI()
    local_app = create_app(Settings(token=TOKEN, host_pane_id="p_host"), cli)
    gw_app = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY))

    with (
        TestClient(gw_app) as gw_client,
        connected_tunnel(gw_client, local_app, push_interval=0.05),
        gw_client.websocket_connect(f"/i/{INSTANCE_ID}/ws?token={TOKEN}") as client_ws,
    ):
        hello = client_ws.receive_json()
        assert hello["type"] == "connected"
        assert hello["instance_id"] == INSTANCE_ID
        assert hello["online"] is True

        # Initial tabs snapshot pushed automatically upon subscription:
        initial_event = client_ws.receive_json()
        assert initial_event["type"] == "event"
        assert initial_event["event"] == "tabs.updated"
        assert len(initial_event["tabs"]) == 3

        # Client can also issue RPC requests over the same WebSocket:
        client_ws.send_json(
            {
                "type": "request",
                "id": "req-1",
                "method": "POST",
                "path": "/api/v1/tabs/t_agent/messages",
                "body": {"text": "ws rpc message"},
            }
        )
        reply = client_ws.receive_json()
        assert reply == {
            "type": "response",
            "id": "req-1",
            "status": 200,
            "body": {
                "tab_id": "t_agent",
                "pane_id": "p_agent",
                "delivery": "queue",
                "receipt": {"queued": True, "pending": 1},
            },
        }

        # Mutate agent_state in Otty and verify the push event arrives:
        cli.panes["p_agent"]["agent_state"] = "awaiting"
        pushed = client_ws.receive_json()
        assert pushed["type"] == "event"
        assert pushed["event"] == "tabs.updated"
        agent_tab = next(t for t in pushed["tabs"] if t["id"] == "t_agent")
        assert agent_tab["agent_state"] == "awaiting"


@pytest.mark.parametrize(
    "path,expected_type,snippet",
    [
        ("/", "text/html", "apple-mobile-web-app-capable"),
        ("/manifest.json", "application/manifest+json", '"display": "standalone"'),
        ("/sw.js", "application/javascript", "otty-pwa-v6"),
        ("/pwa/styles.css", "text/css", "overscroll-behavior: none"),
        ("/pwa/app.js", "application/javascript", "otty_pwa_state_v1"),
        ("/pwa/icon.svg", "image/svg+xml", "<svg"),
    ],
)
def test_pwa_module_mounted_on_gateway_and_local_server(path, expected_type, snippet):
    gw_app = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY))
    local_app = create_app(Settings(token=TOKEN, host_pane_id="p_host"), FakeCLI())

    for app in (gw_app, local_app):
        with TestClient(app) as client:
            res = client.get(path)
            assert res.status_code == 200
            assert expected_type in res.headers["content-type"]
            assert snippet in res.text
