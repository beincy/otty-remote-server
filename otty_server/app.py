import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi import Path as APIPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from gateway.asr import AsrRequest, AsrResult, AsrSettings, transcribe
from gateway.pwa import mount_pwa

from .cli import EXIT_TIMEOUT, OttyCLI, OttyError
from .push import PushManager, PushStatusOut, PushSubscriptionIn, PushUnsubscribeIn
from .screens import ScreenPage, Screens

MIN_TOKEN_LENGTH = 16
TRUTHY = {"1", "true", "yes", "on"}

# Keys a remote client may press, e.g. to answer an agent's approval menu.
# Names follow `otty pane send-keys` (Esc, not Escape; Ctrl+C, not C-c).
# Modified arrow keys not natively named in `otty pane send-keys` (e.g. Option+Up)
# are sent as raw terminal escape sequences via `otty pane send-text`.
RAW_KEY_SEQUENCES = {
    "Option+Up": "\x1b[1;3A",
    "Alt+Up": "\x1b[1;3A",
    "Option+Down": "\x1b[1;3B",
    "Alt+Down": "\x1b[1;3B",
}

NAMED_KEYS = {
    "Enter",
    "Tab",
    "Esc",
    "Backspace",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Ctrl+C",
    *RAW_KEY_SEQUENCES.keys(),
}


@dataclass(frozen=True)
class Settings:
    token: str
    host_pane_id: str
    cors_origins: tuple[str, ...] = ()
    # Typing into a plain shell is remote command execution; opt in explicitly.
    allow_shell_input: bool = False

    def __post_init__(self):
        if len(self.token.strip()) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"Set OTTY_SERVER_TOKEN to a random secret of at least "
                f"{MIN_TOKEN_LENGTH} characters before starting the server"
            )
        if not self.host_pane_id:
            raise ValueError(
                "Start the server inside a local Otty pane (OTTY_PANE_ID is missing)"
            )

    @classmethod
    def from_env(cls):
        if os.getenv("OTTY_REMOTE") or os.getenv("SSH_CONNECTION"):
            raise ValueError(
                "Start the server on the computer running Otty, inside a local Otty pane"
            )
        pane = os.getenv("OTTY_PANE_ID", "")
        return cls(
            token=os.getenv("OTTY_SERVER_TOKEN", ""),
            host_pane_id=pane if not pane or pane.startswith("p_") else f"p_{pane}",
            cors_origins=tuple(
                x.strip()
                for x in os.getenv("OTTY_SERVER_CORS_ORIGINS", "").split(",")
                if x.strip()
            ),
            allow_shell_input=os.getenv("OTTY_SERVER_ALLOW_SHELL_INPUT", "").lower()
            in TRUTHY,
        )


class Tab(BaseModel):
    id: str
    title: str = ""
    window_id: str = ""
    index: int = 0
    active: bool = False
    cwd: str = ""
    process: str = ""
    pane_count: int = 0
    pane_id: str = Field(
        default="", description="The tab's active pane; input and screen target it"
    )
    agent: str = ""
    agent_session_id: str = ""
    agent_state: str = Field(
        default="",
        description="processing / idle / awaiting / unknown from Otty hooks; "
        "starting = launched by this server, not reported yet; empty = no agent",
    )
    registered: bool = Field(
        default=False, description="Otty hooks identified the agent"
    )
    protected: bool = Field(default=False, description="Contains the server itself")
    writable: bool = Field(
        default=False, description="This server accepts input for it"
    )


class TabList(BaseModel):
    tabs: list[Tab]


class StartTab(BaseModel):
    cwd: str
    command: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="e.g. claude / codex / agy; omit for a plain shell",
    )
    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("command", "title", "cwd")
    @classmethod
    def no_null_bytes(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("NUL bytes are not allowed")
        return value

    @field_validator("cwd")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(
                "cwd must be an existing absolute directory on the Otty host"
            )
        return str(path.resolve())


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=32768)
    delivery: Literal["queue", "now"] = Field(
        default="queue",
        description="queue: sent once the agent's turn ends; now: steer a working agent",
    )

    @field_validator("text")
    @classmethod
    def no_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL bytes are not allowed")
        return value


class Keys(BaseModel):
    keys: list[str] = Field(
        min_length=1,
        max_length=20,
        description=f"Each item is one of {sorted(NAMED_KEYS)} or a single printable character",
        examples=[["Down", "Enter"], ["1"], ["Esc"]],
    )

    @field_validator("keys")
    @classmethod
    def known_keys(cls, value: list[str]) -> list[str]:
        for key in value:
            if key not in NAMED_KEYS and not (len(key) == 1 and key.isprintable()):
                raise ValueError(f"Unsupported key {key!r}")
        return value


class StreamType(BaseModel):
    backspaces: int = Field(
        default=0,
        ge=0,
        le=500,
        description="Number of Backspace keys to send before typing text",
    )
    text: str = Field(
        default="",
        max_length=4096,
        description="Live text to type into the pane without pressing Enter",
    )
    submit: bool = Field(
        default=False,
        description="Press Enter after applying backspaces and text",
    )
    settle_ms: int = Field(
        default=80,
        ge=0,
        le=500,
        description="Wait before capturing screen so TUI autocomplete menus render",
    )
    limit: int = Field(default=60, ge=1, le=200)

    @field_validator("text")
    @classmethod
    def no_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL bytes are not allowed")
        return value


class Submission(BaseModel):
    tab_id: str
    pane_id: str
    delivery: str
    receipt: dict | None = None


class WaitResult(BaseModel):
    done: bool = Field(description="false: timed out, or the agent is awaiting a human")
    tab: Tab


TabId = Annotated[str, APIPath(pattern=r"^t_[A-Za-z0-9_-]+$")]


def create_app(
    settings: Settings | None = None,
    cli: OttyCLI | None = None,
    asr_settings: AsrSettings | None = None,
    push_manager: PushManager | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    cli = cli or OttyCLI()
    asr_settings = asr_settings or AsrSettings.from_env()
    push_manager = push_manager or PushManager()
    screens = Screens(cli)
    # tab_id -> command, for tabs this server launched whose agent hooks have not
    # reported yet. Pruned when the agent registers or the tab disappears.
    launched: dict[str, str] = {}
    writes = asyncio.Lock()  # Small single-process MVP: serialize mutating CLI calls.
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), settings.token.encode()
        ):
            raise HTTPException(
                401,
                "Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        screens.snapshots.clear()

    app = FastAPI(
        title="Otty Server",
        version="0.3.0",
        lifespan=lifespan,
        description="Remote Otty control by tab: list / open / close tabs, read a tab's "
        "screen, send it a message or keys. All API calls require a bearer token.",
    )
    api = APIRouter(dependencies=[Depends(authenticate)])
    mount_pwa(app)
    app.state.screens = screens
    app.state.cli = cli
    app.state.push = push_manager
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.exception_handler(OttyError)
    async def otty_error(_request, exc: OttyError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    def tab_model(tab: dict, panes: list[dict]) -> Tab:
        own = [p for p in panes if p.get("tab_id") == tab["id"]]
        pane = next((p for p in own if p.get("active")), own[0] if own else {})
        agent = pane.get("agent") or ""
        state = pane.get("agent_state") or ""
        registered = bool(agent)
        if registered:
            launched.pop(tab["id"], None)
            if not state or state == "unknown":
                state = "idle"
        elif tab["id"] in launched:
            agent, state = launched[tab["id"]], "starting"
        protected = any(p.get("id") == settings.host_pane_id for p in own)
        fields = {k: v for k, v in tab.items() if k in Tab.model_fields}
        return Tab(
            **fields,
            pane_id=pane.get("id", ""),
            agent=agent,
            agent_session_id=pane.get("agent_session_id") or "",
            agent_state=state,
            registered=registered,
            protected=protected,
            writable=not protected and (bool(agent) or settings.allow_shell_input),
        )

    async def get_tab(tab_id: str) -> Tab:
        data = await cli.call("tab", "show", "--tab", tab_id)
        # The CLI also accepts abbreviated selectors; HTTP IDs must be exact.
        if not isinstance(data, dict) or data.get("id") != tab_id:
            raise HTTPException(404, "Tab not found; use its complete ID")
        return tab_model(data, await cli.call("pane", "list", "--tab", tab_id))

    def ensure_writable(tab: Tab):
        if tab.protected:
            raise HTTPException(409, "This tab runs the server itself and is protected")
        if not tab.pane_id:
            raise HTTPException(409, "The tab has no pane to receive input")
        if not tab.writable:
            raise HTTPException(
                403,
                "Input to plain shell tabs is disabled; "
                "set OTTY_SERVER_ALLOW_SHELL_INPUT=1 to allow it",
            )

    @api.get("/api/v1/health", tags=["System"])
    async def health():
        host = await cli.call("pane", "show", "--pane", settings.host_pane_id)
        return {
            "status": "ok",
            "host_pane_id": host["id"],
            "allow_shell_input": settings.allow_shell_input,
        }

    @api.get("/api/v1/tabs", response_model=TabList, tags=["Tabs"])
    async def list_tabs():
        tabs, panes = await asyncio.gather(
            cli.call("tab", "list"), cli.call("pane", "list")
        )
        alive = {t["id"] for t in tabs}
        for tab_id in [t for t in launched if t not in alive]:
            del launched[tab_id]
        models = [tab_model(t, panes) for t in tabs]
        if push_manager.has_subscriptions:
            await push_manager.observe_tabs(models, cli=cli)
        return TabList(tabs=models)

    @api.post("/api/v1/tabs", response_model=Tab, status_code=201, tags=["Tabs"])
    async def open_tab(body: StartTab):
        async with writes:
            host = await cli.call("pane", "show", "--pane", settings.host_pane_id)
            args = ["tab", "new", "--window", host["window_id"], "--cwd", body.cwd]
            if body.command:
                args.extend(["--command", body.command])
            if body.title:
                args.extend(["--title", body.title])
            created = await cli.call(*args, "--no-focus")
            tab_id = created["id"]
            if body.command:
                launched[tab_id] = body.command
            return await get_tab(tab_id)

    @api.get("/api/v1/tabs/{tab_id}", response_model=Tab, tags=["Tabs"])
    async def show_tab(tab_id: TabId):
        return await get_tab(tab_id)

    @api.delete("/api/v1/tabs/{tab_id}", tags=["Tabs"])
    async def close_tab(tab_id: TabId):
        async with writes:
            tab = await get_tab(tab_id)
            if tab.protected:
                raise HTTPException(
                    409, "This tab runs the server itself and is protected"
                )
            result = await cli.call("tab", "close", "--tab", tab_id, "--force")
            launched.pop(tab_id, None)
            return result

    @api.get("/api/v1/tabs/{tab_id}/screen", response_model=ScreenPage, tags=["Tabs"])
    async def tab_screen(
        tab_id: TabId,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[str | None, Query(max_length=200)] = None,
    ):
        tab = await get_tab(tab_id)
        if not tab.pane_id:
            raise HTTPException(409, "The tab has no pane to read")
        page = await screens.page(
            tab.pane_id,
            limit,
            before,
            agent=tab.agent,
            agent_session_id=tab.agent_session_id,
            cwd=tab.cwd,
        )
        page.tab_id, page.agent, page.agent_state = tab.id, tab.agent, tab.agent_state
        return page

    @api.post(
        "/api/v1/tabs/{tab_id}/messages", response_model=Submission, tags=["Tabs"]
    )
    async def send_message(tab_id: TabId, body: Message):
        async with writes:
            tab = await get_tab(tab_id)
            ensure_writable(tab)
            if tab.agent_state == "awaiting":
                # A queued message would wait behind the question forever, and
                # `now` refuses to type into it. The human must answer first.
                raise HTTPException(
                    409,
                    "The agent is waiting for a human answer; read the screen and "
                    "reply with POST /keys",
                )
            try:
                receipt = await cli.call(
                    "pane",
                    "send-text",
                    "--pane",
                    tab.pane_id,
                    "--stdin",
                    "--no-escape",
                    f"--submit={body.delivery}",
                    "--wait-secs",
                    "1",
                    text=body.text,
                )
            except OttyError as exc:
                if exc.exit_code == EXIT_TIMEOUT:
                    raise HTTPException(
                        409,
                        "Not sent: the agent stopped on a question; answer it with "
                        "POST /keys first",
                    ) from exc
                raise
        return Submission(
            tab_id=tab.id, pane_id=tab.pane_id, delivery=body.delivery, receipt=receipt
        )

    @api.post("/api/v1/tabs/{tab_id}/keys", response_model=Submission, tags=["Tabs"])
    async def send_keys(tab_id: TabId, body: Keys):
        async with writes:
            tab = await get_tab(tab_id)
            ensure_writable(tab)
            receipt = None
            normal_parts: list[str] = []
            for k in body.keys:
                if k in RAW_KEY_SEQUENCES:
                    if normal_parts:
                        receipt = await cli.call(
                            "pane", "send-keys", "--pane", tab.pane_id, *normal_parts
                        )
                        normal_parts = []
                    receipt = await cli.call(
                        "pane",
                        "send-text",
                        "--pane",
                        tab.pane_id,
                        "--stdin",
                        "--no-escape",
                        text=RAW_KEY_SEQUENCES[k],
                    )
                else:
                    normal_parts.append(f"key:{k}" if k in NAMED_KEYS else k)
            if normal_parts:
                # No `--` separator: OttyCLI appends `--json`, which would then be typed
                # as text. Parts are single characters or `key:` names, never options
                # (a lone "-" is a positional to the CLI).
                receipt = await cli.call(
                    "pane", "send-keys", "--pane", tab.pane_id, *normal_parts
                )
        return Submission(
            tab_id=tab.id,
            pane_id=tab.pane_id,
            delivery="keys",
            receipt=receipt if isinstance(receipt, dict) else None,
        )

    @api.post("/api/v1/tabs/{tab_id}/type", response_model=ScreenPage, tags=["Tabs"])
    async def stream_type(tab_id: TabId, body: StreamType):
        """Stream live edits (backspaces + text) into the pane and return the updated screen."""
        async with writes:
            tab = await get_tab(tab_id)
            ensure_writable(tab)
            remaining = body.backspaces
            while remaining > 0:
                chunk = min(remaining, 50)
                await cli.call(
                    "pane",
                    "send-keys",
                    "--pane",
                    tab.pane_id,
                    *(["key:Backspace"] * chunk),
                )
                remaining -= chunk
            if body.text:
                await cli.call(
                    "pane",
                    "send-text",
                    "--pane",
                    tab.pane_id,
                    "--stdin",
                    "--no-escape",
                    "--raw-newlines",
                    text=body.text,
                )
            if body.submit:
                await cli.call("pane", "send-keys", "--pane", tab.pane_id, "key:Enter")
        if body.settle_ms > 0 and (body.backspaces or body.text or body.submit):
            await asyncio.sleep(body.settle_ms / 1000)
        page = await screens.page(
            tab.pane_id,
            body.limit,
            None,
            agent=tab.agent,
            agent_session_id=tab.agent_session_id,
            cwd=tab.cwd,
        )
        page.tab_id, page.agent, page.agent_state = tab.id, tab.agent, tab.agent_state
        return page

    @api.get("/api/v1/tabs/{tab_id}/wait", response_model=WaitResult, tags=["Tabs"])
    async def wait_tab(tab_id: TabId, timeout: Annotated[int, Query(ge=1, le=60)] = 25):
        """Long-poll until the tab is idle (agent idle / shell back at a prompt)."""
        tab = await get_tab(tab_id)
        if tab.agent_state == "awaiting":
            return WaitResult(done=False, tab=tab)
        try:
            await cli.call(
                "pane",
                "wait",
                "--tab",
                tab_id,
                "--timeout-secs",
                str(timeout),
                "--settle-ms",
                "800",
                timeout=timeout + 10,
            )
            done = True
        except OttyError as exc:
            if exc.exit_code != EXIT_TIMEOUT:
                raise
            done = False
        tab = await get_tab(tab_id)
        return WaitResult(done=done and tab.agent_state != "awaiting", tab=tab)

    @api.post("/api/v1/asr", response_model=AsrResult, tags=["Voice"])
    async def speech_to_text(body: AsrRequest):
        """Hold-to-talk voice input: transcribe a short recording to text."""
        return await transcribe(asr_settings, body)

    @api.get("/api/v1/push/status", response_model=PushStatusOut, tags=["Push"])
    async def push_status():
        """Return VAPID public key and whether Web Push notifications are enabled."""
        return push_manager.status()

    @api.post("/api/v1/push/subscribe", response_model=PushStatusOut, tags=["Push"])
    async def push_subscribe(body: PushSubscriptionIn):
        """Register or update a Web Push subscription for busy->idle tab notifications."""
        res = push_manager.subscribe(body)
        try:
            tabs, panes = await asyncio.gather(
                cli.call("tab", "list"), cli.call("pane", "list")
            )
            await push_manager.observe_tabs([tab_model(t, panes) for t in tabs], cli=cli)
        except Exception:
            pass
        return res

    @api.post("/api/v1/push/unsubscribe", response_model=PushStatusOut, tags=["Push"])
    async def push_unsubscribe(body: PushUnsubscribeIn | None = None):
        """Remove a Web Push subscription (or clear all when endpoint is omitted)."""
        return push_manager.unsubscribe(body.endpoint if body else None)

    @api.post("/api/v1/push/test", tags=["Push"])
    async def push_test():
        """Send a test notification to all registered Web Push subscriptions."""
        if not push_manager.has_subscriptions:
            raise HTTPException(409, "尚未开启消息推送通知，请先在设置中开启")
        sent = await push_manager.send_notification(
            title="🔔 Otty 消息通知已开启",
            body="当任意 Agent 会话从忙碌变为空闲（或等待确认）时，你将收到通知。",
            tag="otty-push-test",
        )
        return {"status": "ok", "sent": sent}

    app.include_router(api)
    return app
