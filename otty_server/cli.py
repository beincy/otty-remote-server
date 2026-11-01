import asyncio
import json
from typing import Any

# Otty CLI exit codes -> HTTP status.
EXIT_STATUSES = {2: 400, 3: 503, 4: 404, 5: 409, 6: 409, 7: 403, 9: 504}
EXIT_TIMEOUT = (
    9  # e.g. `pane wait --timeout-secs` or `--submit=now` gave up, nothing sent
)


class OttyError(Exception):
    def __init__(
        self, message: str, status_code: int = 502, exit_code: int | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        # The CLI's own exit code; None when the CLI never answered (missing, killed).
        self.exit_code = exit_code


class OttyCLI:
    def __init__(self, executable: str = "otty", timeout: float = 15):
        self.executable = executable
        self.timeout = timeout

    async def call(
        self, *args: str, text: str | None = None, timeout: float | None = None
    ) -> Any:
        """Invoke the CLI without an outer shell; message bodies use stdin."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                "--json",
                stdin=asyncio.subprocess.PIPE
                if text is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise OttyError("Otty CLI was not found on PATH", 503) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(None if text is None else text.encode("utf-8")),
                self.timeout if timeout is None else timeout,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise OttyError(
                "Otty CLI timed out; the operation may already have been submitted. "
                "Check the target before retrying.",
                504,
            ) from exc

        try:
            payload = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if (
            process.returncode != 0
            or not isinstance(payload, dict)
            or not payload.get("ok")
        ):
            message = stderr.decode("utf-8", errors="replace").strip()
            if isinstance(payload, dict):
                message = str(
                    payload.get("error") or message or "Otty operation failed"
                )
            raise OttyError(
                message or "Invalid response from Otty CLI",
                EXIT_STATUSES.get(process.returncode, 502),
                process.returncode,
            )
        return payload.get("data")
