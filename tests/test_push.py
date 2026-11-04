import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from otty_server.app import Settings, create_app
from otty_server.push import (
    PushManager,
    VapidKeyPair,
    encrypt_webpush_aes128gcm,
)

from test_api import AUTH, TOKEN, FakeCLI


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_browser_subscription() -> dict:
    ua_priv = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua_priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    auth = os.urandom(16)
    return {
        "endpoint": "https://web.push.apple.com/QTEST123456",
        "keys": {
            "p256dh": _b64url(ua_pub),
            "auth": _b64url(auth),
        },
    }


def test_vapid_and_aes128gcm_encryption():
    vapid = VapidKeyPair.generate()
    jwt = vapid.sign_vapid_jwt("https://web.push.apple.com/QTEST123456")
    assert len(jwt.split(".")) == 3

    sub = _make_browser_subscription()
    body = encrypt_webpush_aes128gcm(
        b'{"title":"ok"}',
        sub["keys"]["p256dh"],
        sub["keys"]["auth"],
    )
    # 16B salt + 4B rs + 1B idlen (65) + 65B pubkey = 86B header + ciphertext
    assert len(body) > 86
    assert body[20] == 65


def test_push_api_and_busy_to_idle_transition(tmp_path, monkeypatch):
    cli = FakeCLI()
    push_mgr = PushManager(state_path=tmp_path / "push_state.json")
    sent_payloads: list[dict] = []

    async def fake_send_notification(*, title: str, body: str, tab_id: str = "", tag: str = "") -> int:
        sent_payloads.append({"title": title, "body": body, "tab_id": tab_id, "tag": tag})
        return 1

    monkeypatch.setattr(push_mgr, "send_notification", fake_send_notification)
    app = create_app(Settings(TOKEN, "p_host"), cli, push_manager=push_mgr)

    with TestClient(app) as client:
        # 1. Initially disabled
        st = client.get("/api/v1/push/status", headers=AUTH).json()
        assert st["enabled"] is False
        assert st["subscribedCount"] == 0
        assert len(st["publicKey"]) >= 80

        # 2. When push is disabled, busy -> idle transitions do NOT trigger notifications
        cli.panes["p_agent"]["agent_state"] = "processing"
        client.get("/api/v1/tabs", headers=AUTH)
        cli.panes["p_agent"]["agent_state"] = "idle"
        client.get("/api/v1/tabs", headers=AUTH)
        assert sent_payloads == []

        # 3. Subscribe from browser
        sub = _make_browser_subscription()
        cli.panes["p_agent"]["agent_state"] = "processing"
        res = client.post("/api/v1/push/subscribe", headers=AUTH, json=sub).json()
        assert res["enabled"] is True
        assert res["subscribedCount"] == 1

        # 4. Now agent transitions from processing -> idle: notification is sent!
        cli.text = "All tests passed\nBuild succeeded\n"
        cli.panes["p_agent"]["agent_state"] = "idle"
        client.get("/api/v1/tabs", headers=AUTH)
        assert len(sent_payloads) == 1
        assert "已完成" in sent_payloads[0]["title"]
        assert "Build succeeded" in sent_payloads[0]["body"]
        assert sent_payloads[0]["tab_id"] == "t_agent"

        # 5. Unsubscribe disables notifications again
        unsub = client.post(
            "/api/v1/push/unsubscribe",
            headers=AUTH,
            json={"endpoint": sub["endpoint"]},
        ).json()
        assert unsub["enabled"] is False
        assert unsub["subscribedCount"] == 0
