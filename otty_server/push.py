from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, Field

logger = logging.getLogger("otty_server.push")

DEFAULT_PUSH_STATE_PATH = Path.home() / ".config" / "otty-server" / "push_state.json"
BUSY_STATES = {"processing", "starting"}
NOTIFY_TARGET_STATES = {"idle", "awaiting"}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text.strip() + "=" * ((4 - len(text.strip()) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=16, max_length=256)
    auth: str = Field(min_length=8, max_length=128)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str | None = Field(default=None, max_length=2048)


class PushStatusOut(BaseModel):
    public_key: str = Field(serialization_alias="publicKey")
    enabled: bool
    subscribed_count: int = Field(serialization_alias="subscribedCount")


@dataclass
class VapidKeyPair:
    private_value: int
    public_key_b64: str

    @classmethod
    def generate(cls) -> VapidKeyPair:
        priv = ec.generate_private_key(ec.SECP256R1())
        priv_val = priv.private_numbers().private_value
        pub_bytes = priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return cls(private_value=priv_val, public_key_b64=_b64url_encode(pub_bytes))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VapidKeyPair:
        priv_bytes = _b64url_decode(str(data["private_key"]))
        priv_val = int.from_bytes(priv_bytes, "big")
        priv = ec.derive_private_key(priv_val, ec.SECP256R1())
        pub_bytes = priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return cls(private_value=priv_val, public_key_b64=_b64url_encode(pub_bytes))

    def to_dict(self) -> dict[str, str]:
        return {
            "private_key": _b64url_encode(self.private_value.to_bytes(32, "big")),
            "public_key": self.public_key_b64,
        }

    def sign_vapid_jwt(self, endpoint: str, subject: str = "mailto:otty@localhost") -> str:
        parsed = urlparse(endpoint)
        aud = f"{parsed.scheme}://{parsed.netloc}"
        header = {"typ": "JWT", "alg": "ES256"}
        claims = {
            "aud": aud,
            "exp": int(time.time()) + 12 * 3600,
            "sub": subject,
        }
        signing_input = (
            f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url_encode(json.dumps(claims, separators=(',', ':')).encode())}"
        )
        priv = ec.derive_private_key(self.private_value, ec.SECP256R1())
        der_sig = priv.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input}.{_b64url_encode(raw_sig)}"


def encrypt_webpush_aes128gcm(
    payload: bytes, p256dh_b64: str, auth_b64: str
) -> bytes:
    """Encrypt payload according to RFC 8291 (Web Push Message Encryption, aes128gcm)."""
    ua_public_bytes = _b64url_decode(p256dh_b64)
    auth_secret = _b64url_decode(auth_b64)
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ua_public_bytes
    )

    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public_bytes = as_private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    shared_secret = as_private.exchange(ec.ECDH(), ua_public)

    key_info = b"WebPush: info\x00" + ua_public_bytes + as_public_bytes
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=key_info,
    ).derive(shared_secret)

    salt = os.urandom(16)
    cek = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)

    # RFC 8188 final record delimiter 0x02
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    rs = (4096).to_bytes(4, "big")
    idlen = bytes([len(as_public_bytes)])
    return salt + rs + idlen + as_public_bytes + ciphertext


def _extract_screen_snippet(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    meaningful: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Skip pure box-drawing / divider lines
        if all(ch in "─━═-_=┌┐└┘├┤┬┴┼╭╮╯╰│┃·•*" for ch in line):
            continue
        meaningful.append(line)
    if not meaningful:
        return ""
    snippet = " / ".join(meaningful[-2:])
    return snippet[:120]


class PushManager:
    """Manages VAPID keys, browser push subscriptions, and busy->idle transition notifications."""

    def __init__(self, state_path: Path | None = None):
        env_path = os.getenv("OTTY_SERVER_PUSH_FILE", "").strip()
        if state_path is not None:
            self.state_path: Path | None = state_path
        elif env_path:
            self.state_path = Path(env_path).expanduser()
        elif "PYTEST_CURRENT_TEST" in os.environ:
            self.state_path = None
        else:
            self.state_path = DEFAULT_PUSH_STATE_PATH
        self.vapid: VapidKeyPair
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self._prev_states: dict[str, str] = {}
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self.state_path is not None and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.vapid = VapidKeyPair.from_dict(data["vapid"])
                subs = data.get("subscriptions") or []
                if isinstance(subs, list):
                    for item in subs:
                        if isinstance(item, dict) and item.get("endpoint"):
                            self.subscriptions[str(item["endpoint"])] = item
                return
            except Exception as exc:
                logger.warning("Failed to load push state (%s), regenerating", exc)

        self.vapid = VapidKeyPair.generate()
        self.subscriptions = {}
        self._save()

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "vapid": self.vapid.to_dict(),
                        "subscriptions": list(self.subscriptions.values()),
                    },
                    handle,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError as exc:
            logger.warning("Failed to persist push state: %s", exc)

    @property
    def has_subscriptions(self) -> bool:
        return bool(self.subscriptions)

    def status(self) -> PushStatusOut:
        return PushStatusOut(
            public_key=self.vapid.public_key_b64,
            enabled=self.has_subscriptions,
            subscribed_count=len(self.subscriptions),
        )

    def subscribe(self, sub: PushSubscriptionIn) -> PushStatusOut:
        self.subscriptions[sub.endpoint] = sub.model_dump()
        self._save()
        return self.status()

    def unsubscribe(self, endpoint: str | None = None) -> PushStatusOut:
        if endpoint:
            self.subscriptions.pop(endpoint, None)
        else:
            self.subscriptions.clear()
        if not self.subscriptions:
            self._prev_states.clear()
        self._save()
        return self.status()

    async def send_notification(
        self,
        *,
        title: str,
        body: str,
        tab_id: str = "",
        tag: str = "otty-agent",
    ) -> int:
        if not self.subscriptions:
            return 0

        payload_bytes = json.dumps(
            {
                "title": title,
                "body": body,
                "tab_id": tab_id,
                "tag": tag,
                "timestamp": int(time.time() * 1000),
            },
            ensure_ascii=False,
        ).encode("utf-8")

        dead_endpoints: list[str] = []
        sent = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            for endpoint, sub in list(self.subscriptions.items()):
                try:
                    keys = sub.get("keys") or {}
                    encrypted = encrypt_webpush_aes128gcm(
                        payload_bytes,
                        str(keys["p256dh"]),
                        str(keys["auth"]),
                    )
                    jwt_token = self.vapid.sign_vapid_jwt(endpoint)
                    headers = {
                        "Authorization": f"vapid t={jwt_token}, k={self.vapid.public_key_b64}",
                        "Content-Encoding": "aes128gcm",
                        "Content-Type": "application/octet-stream",
                        "TTL": "86400",
                        "Urgency": "high",
                    }
                    res = await client.post(endpoint, content=encrypted, headers=headers)
                    if res.status_code in (200, 201, 202, 204):
                        sent += 1
                    elif res.status_code in (404, 410):
                        dead_endpoints.append(endpoint)
                except Exception as exc:
                    logger.warning("WebPush delivery failed for %s: %s", endpoint[:48], exc)

        if dead_endpoints:
            for ep in dead_endpoints:
                self.subscriptions.pop(ep, None)
            self._save()
        return sent

    async def observe_tabs(self, tabs: list[Any], cli: Any = None) -> None:
        """Track tab state transitions and push notifications when a busy tab becomes idle/awaiting.
        Does nothing if the user has not enabled push notifications."""
        if not self.has_subscriptions:
            return

        first_observation = not bool(self._prev_states)
        current_ids: set[str] = set()
        transitions: list[tuple[str, str, str, str]] = []

        for item in tabs:
            if isinstance(item, dict):
                tab_id = str(item.get("id") or "")
                state = str(item.get("agent_state") or "")
                title = (
                    str(item.get("title") or "")
                    or str(item.get("agent") or "")
                    or str(item.get("process") or "会话")
                )
                pane_id = str(item.get("pane_id") or "")
            else:
                tab_id = str(getattr(item, "id", "") or "")
                state = str(getattr(item, "agent_state", "") or "")
                title = (
                    str(getattr(item, "title", "") or "")
                    or str(getattr(item, "agent", "") or "")
                    or str(getattr(item, "process", "") or "会话")
                )
                pane_id = str(getattr(item, "pane_id", "") or "")

            if not tab_id:
                continue
            current_ids.add(tab_id)
            prev = self._prev_states.get(tab_id)
            self._prev_states[tab_id] = state

            if (
                not first_observation
                and prev in BUSY_STATES
                and state in NOTIFY_TARGET_STATES
            ):
                transitions.append((tab_id, title, state, pane_id))

        for stale_id in [tid for tid in self._prev_states if tid not in current_ids]:
            del self._prev_states[stale_id]

        for tab_id, title, new_state, pane_id in transitions:
            snippet = ""
            if cli is not None and pane_id:
                try:
                    raw = await cli.call(
                        "pane", "capture", "--pane", pane_id, "--lines", "12"
                    )
                    snippet = _extract_screen_snippet(raw)
                except Exception:
                    snippet = ""
            if new_state == "awaiting":
                notif_title = f"⚡ {title} 等待确认"
                notif_body = snippet or "Agent 正在等待你的按键确认，点击前往处理"
            else:
                notif_title = f"✅ {title} 已完成"
                notif_body = snippet or "Agent 已从忙碌变为空闲，点击查看最新回复"

            asyncio.create_task(
                self.send_notification(
                    title=notif_title,
                    body=notif_body,
                    tab_id=tab_id,
                    tag=f"otty-tab-{tab_id}",
                )
            )
