import asyncio
import json
import logging
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import websockets
from fastapi import FastAPI

from .gateway import MIN_SECRET_LENGTH, default_instance_id

logger = logging.getLogger("otty_server.tunnel")
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "otty-server" / "instance.json"


@dataclass(frozen=True)
class InstanceCredentials:
    instance_id: str
    token: str
    path: Path | None = None
    created: bool = False


def load_or_create_credentials(
    token: str | None = None,
    instance_id: str | None = None,
    path: Path | None = None,
) -> InstanceCredentials:
    """Use explicit env credentials when given; otherwise load or create a 0600 file."""
    env_token = (
        token if token is not None else os.getenv("OTTY_SERVER_TOKEN", "")
    ).strip()
    env_id = (
        instance_id if instance_id is not None else os.getenv("OTTY_INSTANCE_ID", "")
    ).strip()
    if env_token:
        if len(env_token) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"OTTY_SERVER_TOKEN must be at least {MIN_SECRET_LENGTH} characters"
            )
        return InstanceCredentials(
            instance_id=env_id or default_instance_id(env_token),
            token=env_token,
        )

    target = (
        path
        or Path(
            os.getenv("OTTY_SERVER_CREDENTIALS_FILE", str(DEFAULT_CREDENTIALS_PATH))
        ).expanduser()
    )
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        saved_token = str(data.get("token", "")).strip()
        saved_id = str(data.get("instance_id", "")).strip()
        if len(saved_token) >= MIN_SECRET_LENGTH:
            return InstanceCredentials(
                instance_id=env_id or saved_id or default_instance_id(saved_token),
                token=saved_token,
                path=target,
                created=False,
            )

    new_token = f"otty_{secrets.token_urlsafe(32)}"
    new_id = env_id or default_instance_id(new_token)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"instance_id": new_id, "token": new_token}, handle, indent=2)
    return InstanceCredentials(
        instance_id=new_id, token=new_token, path=target, created=True
    )


def build_agent_ws_url(
    gateway_url: str,
    *,
    register_key: str = "",
    token: str = "",
    instance_id: str = "",
) -> str:
    parsed = urlparse(gateway_url.strip())
    scheme_map = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    if parsed.scheme not in scheme_map or not parsed.netloc:
        raise ValueError(
            "OTTY_GATEWAY_URL must be an http(s):// or ws(s):// URL, "
            f"got {gateway_url!r}"
        )
    path = parsed.path.rstrip("/")
    if not path.endswith("/ws/agent"):
        path = f"{path}/ws/agent"

    params: dict[str, str] = {}
    if parsed.query:
        params.update(parse_qsl(parsed.query))
    if register_key:
        params["register_key"] = register_key
    if token:
        params["token"] = token
    if instance_id:
        params["instance_id"] = instance_id

    query = urlencode(params) if params else parsed.query
    return urlunparse(parsed._replace(scheme=scheme_map[parsed.scheme], path=path, query=query))


class TunnelClient:
    """Outbound WebSocket bridge from the local Otty Server to the public Gateway."""

    def __init__(
        self,
        app: FastAPI,
        *,
        gateway_url: str,
        register_key: str,
        token: str,
        instance_id: str | None = None,
        push_interval: float = 2.0,
    ):
        if len(register_key.strip()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"Set OTTY_GATEWAY_REGISTER_KEY (or GATEWAY_REGISTER_KEY) to at least "
                f"{MIN_SECRET_LENGTH} characters"
            )
        self.app = app
        self.register_key = register_key.strip()
        self.token = token.strip()
        self.instance_id = (instance_id or default_instance_id(self.token)).strip()
        self.push_interval = push_interval
        # 注意：先赋值 register_key / token / instance_id，再拼包含 query 参数的 ws_url，
        # 兜底解决 cpolar / 反代在 WebSocket Upgrade 握手时丢弃自定义 Header 的问题
        self.ws_url = build_agent_ws_url(
            gateway_url,
            register_key=self.register_key,
            token=self.token,
            instance_id=self.instance_id,
        )
        self.subscribers = 0
        self._last_tabs_snapshot: str | None = None

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Instance-Id": self.instance_id,
            "X-Register-Key": self.register_key,
        }

    async def execute_local(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        query: str = "",
        body: Any = None,
    ) -> tuple[int, Any]:
        url = path if not query else f"{path}?{query}"
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json=body if body is not None else None,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        return response.status_code, payload

    async def poll_tabs_once(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        status, payload = await self.execute_local(client, "GET", "/api/v1/tabs")
        if status != 200 or not isinstance(payload, dict):
            return None
        tabs = payload.get("tabs", [])
        serialized = json.dumps(tabs, sort_keys=True, ensure_ascii=False)
        if serialized == self._last_tabs_snapshot:
            return None
        self._last_tabs_snapshot = serialized
        return {"type": "event", "event": "tabs.updated", "tabs": tabs}

    async def run_session(self, send_json, recv_json):
        """Run a single connected tunnel session using abstract send/recv callbacks."""
        transport = httpx.ASGITransport(app=self.app)
        tasks: set[asyncio.Task] = set()
        self._last_tabs_snapshot = None

        async with httpx.AsyncClient(
            transport=transport, base_url="http://otty.local", timeout=75.0
        ) as client:

            async def handle_request(msg: dict[str, Any]):
                req_id = msg.get("id")
                status, payload = await self.execute_local(
                    client,
                    str(msg.get("method", "GET")),
                    str(msg.get("path", "/api/v1/tabs")),
                    str(msg.get("query", "")),
                    msg.get("body"),
                )
                await send_json(
                    {
                        "type": "response",
                        "id": req_id,
                        "status": status,
                        "body": payload,
                    }
                )
                if self.subscribers > 0:
                    event = await self.poll_tabs_once(client)
                    if event is not None:
                        await send_json(event)

            async def watch_tabs():
                while True:
                    await asyncio.sleep(self.push_interval)
                    if self.subscribers <= 0:
                        continue
                    with suppress(OSError, RuntimeError, httpx.HTTPError):
                        event = await self.poll_tabs_once(client)
                        if event is not None:
                            await send_json(event)

            watcher = asyncio.create_task(watch_tabs())
            try:
                while True:
                    msg = await recv_json()
                    if not isinstance(msg, dict):
                        continue
                    kind = msg.get("type")
                    if kind == "close":
                        break
                    if kind == "request":
                        task = asyncio.create_task(handle_request(msg))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif kind in ("registered", "subscribers"):
                        self.subscribers = int(
                            msg.get("subscribers", msg.get("count", 0))
                        )
                        if self.subscribers > 0:
                            self._last_tabs_snapshot = None
                            event = await self.poll_tabs_once(client)
                            if event is not None:
                                await send_json(event)
            finally:
                watcher.cancel()
                for task in list(tasks):
                    task.cancel()
                await asyncio.gather(watcher, *tasks, return_exceptions=True)

    async def serve_forever(self, stop_event: asyncio.Event | None = None):
        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self.headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    logger.info(
                        "Connected to gateway %s as instance %s",
                        self.ws_url,
                        self.instance_id,
                    )
                    backoff = 1.0

                    async def send_json(payload: dict[str, Any]):
                        await ws.send(json.dumps(payload, ensure_ascii=False))

                    async def recv_json() -> dict[str, Any]:
                        raw = await ws.recv()
                        return json.loads(raw)

                    await self.run_session(send_json, recv_json)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, websockets.WebSocketException) as exc:
                logger.warning(
                    "Gateway tunnel disconnected (%s); retrying in %.1fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
