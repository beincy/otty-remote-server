from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.routing import Mount

from .pwa import mount_pwa

MIN_SECRET_LENGTH = 16
FORWARDED_METHODS = ["GET", "POST", "DELETE"]


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def default_instance_id(token: str) -> str:
    return f"inst_{token_digest(token)[:12]}"


@dataclass(frozen=True)
class GatewaySettings:
    register_key: str
    cors_origins: tuple[str, ...] = ()

    def __post_init__(self):
        if len(self.register_key.strip()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"Set OTTY_GATEWAY_REGISTER_KEY (or GATEWAY_REGISTER_KEY) to at least "
                f"{MIN_SECRET_LENGTH} characters before starting the gateway"
            )

    @classmethod
    def from_env(cls) -> GatewaySettings:
        key = (
            os.getenv("OTTY_GATEWAY_REGISTER_KEY")
            or os.getenv("GATEWAY_REGISTER_KEY")
            or ""
        )
        origins = (
            os.getenv("OTTY_GATEWAY_CORS_ORIGINS")
            or os.getenv("OTTY_SERVER_CORS_ORIGINS")
            or ""
        )
        return cls(
            register_key=key,
            cors_origins=tuple(x.strip() for x in origins.split(",") if x.strip()),
        )


@dataclass
class InstanceSession:
    instance_id: str
    token_hash: str
    websocket: WebSocket
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    subscribers: set[WebSocket] = field(default_factory=set)
    last_tabs: list[dict[str, Any]] | None = None

    def matches_token(self, candidate: str) -> bool:
        return secrets.compare_digest(self.token_hash, token_digest(candidate))

    async def broadcast(self, payload: dict[str, Any]):
        dead: list[WebSocket] = []
        for ws in list(self.subscribers):
            try:
                await ws.send_json(payload)
            except (OSError, RuntimeError, WebSocketDisconnect):
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)


class GatewayHub:
    def __init__(self):
        self.by_id: dict[str, InstanceSession] = {}
        self.by_token: dict[str, InstanceSession] = {}
        # Keep subscribers connected even while an instance reconnects.
        self.waiting_subscribers: dict[str, set[WebSocket]] = {}

    async def register(
        self, instance_id: str, token: str, websocket: WebSocket
    ) -> InstanceSession:
        digest = token_digest(token)
        old = self.by_id.get(instance_id) or self.by_token.get(digest)
        carried_subs: set[WebSocket] = set(
            self.waiting_subscribers.pop(instance_id, set())
        )
        if old is not None:
            carried_subs.update(old.subscribers)
            await self.unregister(old, reason="Replaced by a newer connection")
            with suppress(OSError, RuntimeError, WebSocketDisconnect):
                await old.websocket.close(code=4009)
        session = InstanceSession(
            instance_id=instance_id,
            token_hash=digest,
            websocket=websocket,
            subscribers=carried_subs,
        )
        self.by_id[instance_id] = session
        self.by_token[digest] = session
        await session.broadcast(
            {"type": "event", "event": "agent.online", "instance_id": instance_id}
        )
        return session

    async def unregister(
        self, session: InstanceSession, reason: str = "Local Otty server disconnected"
    ):
        if self.by_id.get(session.instance_id) is session:
            del self.by_id[session.instance_id]
        if self.by_token.get(session.token_hash) is session:
            del self.by_token[session.token_hash]
        for fut in list(session.pending.values()):
            if not fut.done():
                fut.set_exception(HTTPException(503, reason))
        session.pending.clear()
        if session.subscribers:
            self.waiting_subscribers.setdefault(session.instance_id, set()).update(
                session.subscribers
            )
            await session.broadcast(
                {
                    "type": "event",
                    "event": "agent.offline",
                    "instance_id": session.instance_id,
                }
            )

    def resolve(self, token: str, instance_id: str | None = None) -> InstanceSession:
        if not token:
            raise HTTPException(
                401,
                "Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if instance_id is not None:
            session = self.by_id.get(instance_id)
            if session is None:
                raise HTTPException(503, f"Instance {instance_id!r} is offline")
            if not session.matches_token(token):
                raise HTTPException(
                    401,
                    "Invalid bearer token for this instance",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return session
        session = self.by_token.get(token_digest(token))
        if session is None:
            raise HTTPException(
                401,
                "No online instance matches this bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return session

    async def forward(
        self,
        session: InstanceSession,
        method: str,
        path: str,
        query: str = "",
        body: Any = None,
    ) -> tuple[int, Any]:
        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        session.pending[req_id] = fut
        # Wait endpoint can block up to 60s; give it headroom.
        timeout = 75.0 if path.endswith("/wait") else 20.0
        try:
            await session.websocket.send_json(
                {
                    "type": "request",
                    "id": req_id,
                    "method": method.upper(),
                    "path": path,
                    "query": query,
                    "body": body,
                }
            )
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            raise HTTPException(
                504, "Timed out waiting for local Otty server response"
            ) from exc
        except WebSocketDisconnect as exc:
            raise HTTPException(503, "Local Otty server disconnected") from exc
        finally:
            session.pending.pop(req_id, None)
        return int(reply.get("status", 502)), reply.get("body")


def _extract_bearer(header_value: str | None) -> str:
    if not header_value:
        return ""
    scheme, _, credentials = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credentials.strip()


def create_gateway_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or GatewaySettings.from_env()
    hub = GatewayHub()
    bearer = HTTPBearer(auto_error=False)

    app = FastAPI(
        title="Otty Gateway",
        version="0.3.0",
        description="Public WebSocket + HTTP gateway for remote Otty Server instances.",
    )
    mount_pwa(app)
    app.state.hub = hub
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=FORWARDED_METHODS,
            allow_headers=["Authorization", "Content-Type", "X-Instance-Id"],
        )

    def get_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is None or not credentials.credentials:
            raise HTTPException(
                401,
                "Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    @app.get("/gateway/health", tags=["Gateway"])
    async def gateway_health():
        return {"status": "ok", "online_instances": len(hub.by_id)}

    @app.websocket("/ws/agent")
    async def agent_tunnel(websocket: WebSocket):
        reg_key = (
            websocket.headers.get("x-register-key")
            or websocket.query_params.get("register_key")
            or ""
        )
        if not reg_key or not secrets.compare_digest(
            reg_key.encode("utf-8"), settings.register_key.encode("utf-8")
        ):
            await websocket.close(code=4401, reason="Invalid register key")
            return

        token = _extract_bearer(
            websocket.headers.get("authorization")
        ) or websocket.query_params.get("token", "")
        if len(token.strip()) < MIN_SECRET_LENGTH:
            await websocket.close(code=4401, reason="Invalid instance token")
            return

        instance_id = (
            websocket.headers.get("x-instance-id")
            or websocket.query_params.get("instance_id")
            or default_instance_id(token)
        )
        await websocket.accept()
        session = await hub.register(instance_id, token, websocket)
        await websocket.send_json(
            {
                "type": "registered",
                "instance_id": instance_id,
                "subscribers": len(session.subscribers),
            }
        )
        try:
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "response":
                    fut = session.pending.get(str(msg.get("id", "")))
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif kind == "event":
                    if msg.get("event") == "tabs.updated" and isinstance(
                        msg.get("tabs"), list
                    ):
                        session.last_tabs = msg["tabs"]
                    await session.broadcast(
                        {**msg, "type": "event", "instance_id": instance_id}
                    )
                elif kind == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister(session)

    async def handle_client_ws(websocket: WebSocket, instance_id: str | None = None):
        token = _extract_bearer(
            websocket.headers.get("authorization")
        ) or websocket.query_params.get("token", "")
        try:
            session = hub.resolve(token, instance_id)
        except HTTPException as exc:
            await websocket.close(
                code=4401 if exc.status_code == 401 else 4503, reason=exc.detail
            )
            return

        await websocket.accept()
        session.subscribers.add(websocket)
        # Tell the local server at least one client is watching so it can push updates.
        with suppress(OSError, RuntimeError, WebSocketDisconnect):
            await session.websocket.send_json(
                {"type": "subscribers", "count": len(session.subscribers)}
            )
        await websocket.send_json(
            {
                "type": "connected",
                "instance_id": session.instance_id,
                "online": True,
                "tabs": session.last_tabs,
            }
        )
        try:
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "ping":
                    await websocket.send_json({"type": "pong"})
                elif kind == "request":
                    req_id = msg.get("id")
                    method = str(msg.get("method", "GET")).upper()
                    path = str(msg.get("path", "/api/v1/tabs"))
                    if not path.startswith("/api/v1/"):
                        path = "/api/v1/" + path.lstrip("/")
                    params = msg.get("params")
                    query = (
                        urlencode(params, doseq=True)
                        if isinstance(params, dict)
                        else str(msg.get("query", ""))
                    )
                    current = hub.by_id.get(session.instance_id)
                    if current is None:
                        await websocket.send_json(
                            {
                                "type": "response",
                                "id": req_id,
                                "status": 503,
                                "body": {"detail": "Instance is offline"},
                            }
                        )
                        continue
                    try:
                        status, payload = await hub.forward(
                            current, method, path, query, msg.get("body")
                        )
                    except HTTPException as exc:
                        status, payload = exc.status_code, {"detail": exc.detail}
                    await websocket.send_json(
                        {
                            "type": "response",
                            "id": req_id,
                            "status": status,
                            "body": payload,
                        }
                    )
        except WebSocketDisconnect:
            pass
        finally:
            session.subscribers.discard(websocket)
            if session.instance_id in hub.waiting_subscribers:
                hub.waiting_subscribers[session.instance_id].discard(websocket)
            with suppress(OSError, RuntimeError, WebSocketDisconnect):
                await session.websocket.send_json(
                    {"type": "subscribers", "count": len(session.subscribers)}
                )

    @app.websocket("/ws/client")
    async def client_ws_by_token(websocket: WebSocket):
        await handle_client_ws(websocket, websocket.query_params.get("instance_id"))

    @app.websocket("/i/{instance_id}/ws")
    async def client_ws_by_instance(websocket: WebSocket, instance_id: str):
        await handle_client_ws(websocket, instance_id)

    async def proxy_http(
        request: Request,
        api_path: str,
        token: str,
        instance_id: str | None,
    ) -> JSONResponse:
        session = hub.resolve(token, instance_id)
        body: Any = None
        raw = await request.body()
        if raw:
            try:
                body = await request.json()
            except ValueError as exc:
                raise HTTPException(400, "Request body must be valid JSON") from exc
        status, payload = await hub.forward(
            session,
            request.method,
            f"/api/v1/{api_path}",
            request.url.query,
            body,
        )
        return JSONResponse(status_code=status, content=payload)

    @app.api_route(
        "/api/v1/{api_path:path}",
        methods=FORWARDED_METHODS,
        tags=["Forwarded API"],
    )
    async def forward_by_token(
        request: Request,
        api_path: str,
        token: Annotated[str, Depends(get_token)],
        x_instance_id: Annotated[str | None, Header()] = None,
    ):
        return await proxy_http(request, api_path, token, x_instance_id)

    @app.api_route(
        "/i/{instance_id}/api/v1/{api_path:path}",
        methods=FORWARDED_METHODS,
        tags=["Forwarded API"],
    )
    async def forward_by_instance(
        request: Request,
        instance_id: str,
        api_path: str,
        token: Annotated[str, Depends(get_token)],
    ):
        return await proxy_http(request, api_path, token, instance_id)

    # Also serve the full application under /otty so reverse proxies that
    # strip the prefix (Express `app.use('/otty', proxy)` + cpolar tunnels)
    # still reach every endpoint, and direct access keeps working at both / and /otty.
    app.router.routes.append(Mount("/otty", app=app.router, name="gateway_subpath"))

    return app
