import copy

import pytest
from fastapi.testclient import TestClient

from otty_server.app import Settings, create_app
from otty_server.cli import OttyError

TOKEN = "test-token-for-api"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.text = "\n".join(f"line {i}" for i in range(155))
        self.wait_error = None
        self.tabs = {
            "t_host": {"id": "t_host", "title": "server", "window_id": "w_host"},
            "t_agent": {"id": "t_agent", "title": "claude-tab", "window_id": "w_host"},
            "t_shell": {"id": "t_shell", "title": "shell", "window_id": "w_host"},
        }
        self.panes = {
            "p_host": {
                "id": "p_host",
                "tab_id": "t_host",
                "window_id": "w_host",
                "active": True,
                "agent": "",
            },
            "p_agent": {
                "id": "p_agent",
                "tab_id": "t_agent",
                "window_id": "w_host",
                "active": True,
                "agent": "Claude Code",
                "agent_state": "idle",
                "agent_session_id": "session-one",
            },
            "p_split": {
                "id": "p_split",
                "tab_id": "t_agent",
                "window_id": "w_host",
                "active": False,
                "agent": "",
            },
            "p_shell": {
                "id": "p_shell",
                "tab_id": "t_shell",
                "window_id": "w_host",
                "active": True,
                "agent": "",
            },
        }

    async def call(self, *args, text=None, timeout=None):
        self.calls.append((args, text))
        command = args[:2]
        if command == ("pane", "show"):
            pane = self.panes.get(args[3])
            if pane is None:
                raise OttyError("No pane matched", 404, 4)
            return copy.deepcopy(pane)
        if command == ("pane", "list"):
            panes = list(self.panes.values())
            if "--tab" in args:
                tab = args[args.index("--tab") + 1]
                panes = [p for p in panes if p["tab_id"] == tab]
            return copy.deepcopy(panes)
        if command == ("tab", "list"):
            return copy.deepcopy(list(self.tabs.values()))
        if command == ("tab", "show"):
            # Like Otty, accept a unique prefix as a selector.
            matches = [t for k, t in self.tabs.items() if k.startswith(args[3])]
            if len(matches) != 1:
                raise OttyError("No tab matched", 404, 4)
            return copy.deepcopy(matches[0])
        if command == ("tab", "new"):
            self.tabs["t_new"] = {"id": "t_new", "title": "new", "window_id": "w_host"}
            self.panes["p_new"] = {
                "id": "p_new",
                "tab_id": "t_new",
                "window_id": "w_host",
                "active": True,
                "agent": "",
            }
            return {"id": "t_new", "index": 3}
        if command == ("tab", "close"):
            tab = args[3]
            del self.tabs[tab]
            self.panes = {k: p for k, p in self.panes.items() if p["tab_id"] != tab}
            return {"id": tab}
        if command == ("pane", "capture"):
            return self.text
        if command == ("pane", "send-text"):
            return {"queued": True, "pending": 1}
        if command == ("pane", "send-keys"):
            return None
        if command == ("pane", "wait"):
            if self.wait_error:
                raise self.wait_error
            return {"duration_ms": 401}
        raise AssertionError(args)


@pytest.fixture
def setup():
    cli = FakeCLI()
    app = create_app(Settings(token=TOKEN, host_pane_id="p_host"), cli)
    with TestClient(app) as client:
        yield client, cli, app


def writes(cli):
    return [
        a
        for a, _ in cli.calls
        if a[:2]
        in {
            ("pane", "send-text"),
            ("pane", "send-keys"),
            ("tab", "close"),
            ("tab", "new"),
        }
    ]


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/health", "GET"),
        ("/api/v1/tabs", "GET"),
        ("/api/v1/tabs", "POST"),
        ("/api/v1/tabs/t_agent", "GET"),
        ("/api/v1/tabs/t_agent", "DELETE"),
        ("/api/v1/tabs/t_agent/screen", "GET"),
        ("/api/v1/tabs/t_agent/messages", "POST"),
        ("/api/v1/tabs/t_agent/keys", "POST"),
        ("/api/v1/tabs/t_agent/type", "POST"),
        ("/api/v1/tabs/t_agent/wait", "GET"),
    ],
)
def test_business_routes_require_token(setup, path, method):
    client, cli, _ = setup
    for headers in [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
    ]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    assert cli.calls == []


@pytest.mark.parametrize("token", ["  ", "short-token"])
def test_weak_token_fails_closed(token):
    with pytest.raises(ValueError, match="OTTY_SERVER_TOKEN"):
        Settings(token=token, host_pane_id="p_host")


def test_list_tabs_merges_title_and_active_pane_state(setup):
    client, _cli, _ = setup
    tabs = {t["id"]: t for t in client.get("/api/v1/tabs", headers=AUTH).json()["tabs"]}
    assert list(tabs) == ["t_host", "t_agent", "t_shell"]
    agent = tabs["t_agent"]
    assert agent["title"] == "claude-tab"
    assert agent["pane_id"] == "p_agent"  # the active pane, not the inactive split
    assert (agent["agent"], agent["agent_state"]) == ("Claude Code", "idle")
    assert agent["registered"] and agent["writable"] and not agent["protected"]
    assert tabs["t_host"]["protected"] and not tabs["t_host"]["writable"]
    assert not tabs["t_shell"]["writable"]


def test_send_message_preserves_text_and_targets_active_pane(setup):
    client, cli, _ = setup
    text = '第一行\n路径 C:\\work\\file，保留 "引号" 和 $()'
    response = client.post(
        "/api/v1/tabs/t_agent/messages", headers=AUTH, json={"text": text}
    )
    assert response.status_code == 200
    assert response.json()["receipt"] == {"queued": True, "pending": 1}
    assert response.json()["pane_id"] == "p_agent"
    args, sent = cli.calls[-1]
    assert sent == text
    assert args[args.index("--pane") + 1] == "p_agent"
    assert "--stdin" in args and "--no-escape" in args and "--submit=queue" in args
    assert text not in args


def test_awaiting_agent_rejects_messages_but_accepts_keys(setup):
    client, cli, _ = setup
    cli.panes["p_agent"]["agent_state"] = "awaiting"
    response = client.post(
        "/api/v1/tabs/t_agent/messages", headers=AUTH, json={"text": "hi"}
    )
    assert response.status_code == 409
    assert "keys" in response.json()["detail"]
    assert writes(cli) == []
    response = client.post(
        "/api/v1/tabs/t_agent/keys", headers=AUTH, json={"keys": ["Down", "Enter"]}
    )
    assert response.status_code == 200
    assert writes(cli)[-1] == (
        "pane",
        "send-keys",
        "--pane",
        "p_agent",
        "key:Down",
        "key:Enter",
    )
    client.post("/api/v1/tabs/t_agent/keys", headers=AUTH, json={"keys": ["1", "-"]})
    assert writes(cli)[-1][-2:] == ("1", "-")


def test_stream_type_backspaces_text_submit_and_screen(setup):
    client, cli, _ = setup
    cli.text = "> /model\n  1. claude-sonnet\n  2. claude-opus\n"
    res = client.post(
        "/api/v1/tabs/t_agent/type",
        headers=AUTH,
        json={"backspaces": 2, "text": "/mod", "submit": True, "settle_ms": 0},
    )
    assert res.status_code == 200
    page = res.json()
    assert page["tab_id"] == "t_agent"
    assert page["lines"][0] == "> /model"
    recorded = writes(cli)
    assert recorded[0] == (
        "pane",
        "send-keys",
        "--pane",
        "p_agent",
        "key:Backspace",
        "key:Backspace",
    )
    assert recorded[1] == (
        "pane",
        "send-text",
        "--pane",
        "p_agent",
        "--stdin",
        "--no-escape",
        "--raw-newlines",
    )
    assert cli.calls[[a for a, _ in cli.calls].index(recorded[1])][1] == "/mod"
    assert recorded[2] == ("pane", "send-keys", "--pane", "p_agent", "key:Enter")


def test_submit_now_refusal_is_reported_as_not_sent(setup):
    client, cli, _ = setup

    async def refuse(*args, **kwargs):
        if args[:2] == ("pane", "send-text"):
            raise OttyError("agent is awaiting", 504, 9)
        return await FakeCLI.call(cli, *args, **kwargs)

    cli.call = refuse
    response = client.post(
        "/api/v1/tabs/t_agent/messages",
        headers=AUTH,
        json={"text": "hi", "delivery": "now"},
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Not sent")


@pytest.mark.parametrize("keys", [[], ["Escape"], ["ab"], ["\n"], ["Enter"] * 21])
def test_keys_validation(setup, keys):
    client, cli, _ = setup
    response = client.post(
        "/api/v1/tabs/t_agent/keys", headers=AUTH, json={"keys": keys}
    )
    assert response.status_code == 422
    assert cli.calls == []


def test_shell_input_is_opt_in():
    for allow, status in [(False, 403), (True, 200)]:
        cli = FakeCLI()
        app = create_app(Settings(TOKEN, "p_host", allow_shell_input=allow), cli)
        with TestClient(app) as client:
            tab = client.get("/api/v1/tabs/t_shell", headers=AUTH).json()
            assert tab["writable"] is allow
            for path, body in [
                ("messages", {"text": "ls"}),
                ("keys", {"keys": ["Enter"]}),
            ]:
                response = client.post(
                    f"/api/v1/tabs/t_shell/{path}", headers=AUTH, json=body
                )
                assert response.status_code == status
            # Reading a shell's screen is always allowed.
            assert (
                client.get("/api/v1/tabs/t_shell/screen", headers=AUTH).status_code
                == 200
            )


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v1/tabs/t_host/messages", {"text": "exit"}),
        ("POST", "/api/v1/tabs/t_host/keys", {"keys": ["Ctrl+C"]}),
        ("DELETE", "/api/v1/tabs/t_host", None),
    ],
)
def test_host_tab_is_protected_even_with_shell_input(method, path, body):
    cli = FakeCLI()
    app = create_app(Settings(TOKEN, "p_host", allow_shell_input=True), cli)
    with TestClient(app) as client:
        assert client.request(method, path, headers=AUTH, json=body).status_code == 409
    assert writes(cli) == []


def test_open_and_close_tab_tracks_starting_agent(setup, tmp_path):
    client, cli, _ = setup
    command = "claude --model example"
    result = client.post(
        "/api/v1/tabs",
        headers=AUTH,
        json={"cwd": str(tmp_path), "command": command, "title": "work"},
    )
    assert result.status_code == 201
    tab = result.json()
    assert (tab["id"], tab["pane_id"]) == ("t_new", "p_new")
    assert (tab["agent"], tab["agent_state"]) == (command, "starting")
    assert tab["registered"] is False and tab["writable"] is True
    creation = next(a for a, _ in cli.calls if a[:2] == ("tab", "new"))
    assert creation[creation.index("--window") + 1] == "w_host"
    assert creation[creation.index("--command") + 1] == command
    assert creation[creation.index("--cwd") + 1] == str(tmp_path)
    assert "--no-focus" in creation
    cli.panes["p_new"].update(
        agent="Claude Code", agent_state="processing", agent_session_id="registered"
    )
    tab = client.get("/api/v1/tabs/t_new", headers=AUTH).json()
    assert tab["registered"] is True and tab["agent"] == "Claude Code"
    assert client.delete("/api/v1/tabs/t_new", headers=AUTH).status_code == 200
    assert client.get("/api/v1/tabs/t_new", headers=AUTH).status_code == 404


def test_launch_marker_is_pruned_when_tab_disappears(setup, tmp_path):
    client, cli, _ = setup
    client.post(
        "/api/v1/tabs", headers=AUTH, json={"cwd": str(tmp_path), "command": "codex"}
    )
    del cli.tabs["t_new"]
    client.get("/api/v1/tabs", headers=AUTH)
    cli.tabs["t_new"] = {"id": "t_new"}
    assert client.get("/api/v1/tabs/t_new", headers=AUTH).json()["agent"] == ""


def test_plain_shell_tab_has_no_agent(setup, tmp_path):
    client, cli, _ = setup
    tab = client.post("/api/v1/tabs", headers=AUTH, json={"cwd": str(tmp_path)}).json()
    assert tab["agent"] == "" and tab["writable"] is False
    creation = next(a for a, _ in cli.calls if a[:2] == ("tab", "new"))
    assert "--command" not in creation


def test_tab_ids_must_be_exact(setup):
    client, cli, _ = setup
    assert client.get("/api/v1/tabs/t_ag", headers=AUTH).status_code == 404
    assert client.delete("/api/v1/tabs/t_ag", headers=AUTH).status_code == 404
    assert client.get("/api/v1/tabs/agent", headers=AUTH).status_code == 422
    assert writes(cli) == []


def test_screen_pages_are_stable_and_report_state(setup):
    client, cli, _ = setup
    original = [f"line {i}" for i in range(55)]
    original[25] = ""
    original[26] = "  indented"
    cli.text = "\n".join(original) + "\n"
    url = "/api/v1/tabs/t_agent/screen"
    first = client.get(url, headers=AUTH, params={"limit": 20}).json()
    assert first["lines"] == original[-20:]
    assert (first["tab_id"], first["pane_id"]) == ("t_agent", "p_agent")
    assert first["agent_state"] == "idle"
    assert first["hasMore"] is True
    cli.text = "new output"
    second = client.get(
        url, headers=AUTH, params={"limit": 20, "before": first["nextBefore"]}
    ).json()
    third = client.get(
        url, headers=AUTH, params={"limit": 20, "before": second["nextBefore"]}
    ).json()
    assert third["lines"] + second["lines"] + first["lines"] == original
    assert third["nextBefore"] is None and third["hasMore"] is False
    assert first["captured_at"] == third["captured_at"]
    assert sum(args[:2] == ("pane", "capture") for args, _ in cli.calls) == 1
    assert client.get(url, headers=AUTH).json()["lines"] == ["new output"]


def test_screen_default_limit_and_blank_bottom_rows(setup):
    client, cli, _ = setup
    cli.text = "\n".join(f"line {i}" for i in range(100)) + "\n" + "   \n" * 40
    page = client.get("/api/v1/tabs/t_agent/screen", headers=AUTH).json()
    assert len(page["lines"]) == 50
    assert page["lines"][-1] == "line 99"


def test_polling_one_tab_keeps_other_cursors_alive(setup):
    client, _cli, _ = setup
    first = client.get(
        "/api/v1/tabs/t_shell/screen", headers=AUTH, params={"limit": 10}
    ).json()
    for _ in range(100):
        client.get("/api/v1/tabs/t_agent/screen", headers=AUTH)
    response = client.get(
        "/api/v1/tabs/t_shell/screen",
        headers=AUTH,
        params={"limit": 10, "before": first["nextBefore"]},
    )
    assert response.status_code == 200


def test_screen_cursor_validation_expiry_and_limits(setup):
    client, _cli, app = setup
    url = "/api/v1/tabs/t_shell/screen"
    assert client.get(url, headers=AUTH, params={"limit": 0}).status_code == 422
    assert client.get(url, headers=AUTH, params={"limit": 201}).status_code == 422
    assert client.get(url, headers=AUTH, params={"before": "bad"}).status_code == 400
    app.state.screens.max_lines = 40
    first = client.get(url, headers=AUTH, params={"limit": 10}).json()
    assert first["historyLimited"] is True
    cursor = first["nextBefore"]
    assert (
        client.get(
            "/api/v1/tabs/t_agent/screen", headers=AUTH, params={"before": cursor}
        ).status_code
        == 400
    )
    next(iter(app.state.screens.snapshots.values())).expires_at = 0
    assert client.get(url, headers=AUTH, params={"before": cursor}).status_code == 410


@pytest.mark.parametrize(
    "text,expected",
    [("", []), ("a", ["a"]), ("a\n\n", ["a"]), ("a\n\n b  \n\n", ["a", "", " b"])],
)
def test_screen_empty_and_trailing_blank_rows(setup, text, expected):
    client, cli, _ = setup
    cli.text = text
    result = client.get("/api/v1/tabs/t_agent/screen", headers=AUTH)
    assert result.json()["lines"] == expected
    assert result.json()["hasMore"] is False


def test_wait_reports_done_timeout_and_awaiting(setup):
    client, cli, _ = setup
    url = "/api/v1/tabs/t_agent/wait"
    result = client.get(url, headers=AUTH, params={"timeout": 5}).json()
    assert result["done"] is True and result["tab"]["agent_state"] == "idle"
    args = next(a for a, _ in cli.calls if a[:2] == ("pane", "wait"))
    assert args[args.index("--tab") + 1] == "t_agent"
    assert args[args.index("--timeout-secs") + 1] == "5"
    cli.wait_error = OttyError("timed out", 504, 9)
    assert client.get(url, headers=AUTH).json()["done"] is False
    cli.wait_error = OttyError("cannot report", 409, 6)
    assert client.get(url, headers=AUTH).status_code == 409
    cli.wait_error = None
    cli.panes["p_agent"]["agent_state"] = "awaiting"
    before = len(cli.calls)
    result = client.get(url, headers=AUTH).json()
    assert result["done"] is False and result["tab"]["agent_state"] == "awaiting"
    assert not any(a[:2] == ("pane", "wait") for a, _ in cli.calls[before:])
    assert client.get(url, headers=AUTH, params={"timeout": 61}).status_code == 422


def test_validation_and_openapi(setup, tmp_path):
    client, _cli, _ = setup
    for cwd in ["relative", str(tmp_path / "missing"), str(tmp_path) + "\x00"]:
        assert (
            client.post("/api/v1/tabs", headers=AUTH, json={"cwd": cwd}).status_code
            == 422
        )
    schema = client.get("/openapi.json").json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert schema["paths"]["/api/v1/tabs"]["get"]["security"] == [{"HTTPBearer": []}]
    assert TOKEN not in str(schema)
    assert client.get("/docs").status_code == 200


def test_otty_errors_keep_status(setup):
    client, cli, _ = setup

    async def unavailable(*args, **kwargs):
        raise OttyError("Input is disabled", 403, 7)

    cli.call = unavailable
    response = client.post(
        "/api/v1/tabs/t_agent/messages", headers=AUTH, json={"text": "hello"}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Input is disabled"}


def test_explicit_cors_preflight():
    app = create_app(Settings(TOKEN, "p_host", ("https://example.com",)), FakeCLI())
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/tabs",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://example.com"
