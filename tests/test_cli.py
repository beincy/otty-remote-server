import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from otty_server.cli import OttyCLI, OttyError


def test_cli_uses_argv_and_stdin():
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b'{"ok":true,"data":{"queued":true}}', b"")
    with patch(
        "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process
    ) as spawn:
        text = "line one\nC:\\path\\file"
        result = asyncio.run(OttyCLI().call("pane", "send-text", "--stdin", text=text))
        assert result == {"queued": True}
        assert spawn.call_args.args == (
            "otty",
            "pane",
            "send-text",
            "--stdin",
            "--json",
        )
        assert "shell" not in spawn.call_args.kwargs
        process.communicate.assert_awaited_once_with(text.encode())


@pytest.mark.parametrize("exit_code,status", [(3, 503), (4, 404), (7, 403), (9, 504)])
def test_cli_errors(exit_code, status):
    process = AsyncMock()
    process.returncode = exit_code
    process.communicate.return_value = (b'{"ok":false,"error":"refused"}', b"")
    with patch(
        "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process
    ):
        with pytest.raises(OttyError) as error:
            asyncio.run(OttyCLI().call("pane", "show"))
        assert error.value.status_code == status
        assert error.value.exit_code == exit_code


def test_cli_timeout_can_be_raised_per_call():
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b'{"ok":true,"data":null}', b"")
    with (
        patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("asyncio.wait_for", new_callable=AsyncMock) as wait_for,
    ):
        wait_for.return_value = (b'{"ok":true,"data":null}', b"")
        asyncio.run(OttyCLI(timeout=15).call("pane", "wait", timeout=70))
        assert wait_for.call_args.args[1] == 70
        asyncio.run(OttyCLI(timeout=15).call("pane", "list"))
        assert wait_for.call_args.args[1] == 15


def test_missing_cli():
    with patch(
        "asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(OttyError, match="PATH") as error:
            asyncio.run(OttyCLI().call("pane", "list"))
        assert error.value.status_code == 503
