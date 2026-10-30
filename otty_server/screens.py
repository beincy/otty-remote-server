import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ScreenPage(BaseModel):
    tab_id: str = ""
    pane_id: str
    agent: str = ""
    agent_state: str = ""
    lines: list[str]
    captured_at: str
    next_before: str | None = Field(serialization_alias="nextBefore")
    has_more: bool = Field(serialization_alias="hasMore")
    history_limited: bool = Field(serialization_alias="historyLimited")


@dataclass
class Snapshot:
    pane_id: str
    lines: list[str]
    captured_at: str
    expires_at: float
    limited: bool


class Screens:
    """Bounded, in-memory screen snapshots so a scroll-back session pages a fixed view.

    Each refresh (no cursor) takes a new snapshot of that pane. Only the newest
    ``per_pane`` snapshots of a pane are kept, so a client that polls the latest
    page does not evict the cursors other panes' readers are paging through.
    """

    def __init__(
        self,
        cli,
        max_lines: int = 2000,
        ttl: float = 300,
        capacity: int = 64,
        per_pane: int = 2,
    ):
        self.cli = cli
        self.max_lines = max_lines
        self.ttl = ttl
        self.capacity = capacity
        self.per_pane = per_pane
        self.snapshots: OrderedDict[str, Snapshot] = OrderedDict()

    def _expire(self):
        now = time.monotonic()
        for key in [k for k, s in self.snapshots.items() if s.expires_at <= now]:
            del self.snapshots[key]

    def _store(self, snapshot: Snapshot) -> str:
        same_pane = [
            k for k, s in self.snapshots.items() if s.pane_id == snapshot.pane_id
        ]
        for key in same_pane[: max(0, len(same_pane) - self.per_pane + 1)]:
            del self.snapshots[key]
        while len(self.snapshots) >= self.capacity:
            self.snapshots.popitem(last=False)  # least recently used
        key = secrets.token_urlsafe(18)
        self.snapshots[key] = snapshot
        return key

    async def _capture(self, pane_id: str) -> Snapshot:
        text = await self.cli.call(
            "pane",
            "capture",
            "--pane",
            pane_id,
            "--scope",
            "scrollback",
            "--lines",
            str(self.max_lines),
        )
        if not isinstance(text, str):
            raise HTTPException(502, "Otty returned an invalid screen")
        lines = text.split("\n") if text else []
        # The grid always includes the empty rows below the prompt / composer; drop
        # them (and per-row padding) so the newest page holds real content. Blank
        # rows between content and indentation are kept.
        lines = [line.rstrip() for line in lines]
        while lines and not lines[-1]:
            lines.pop()
        return Snapshot(
            pane_id=pane_id,
            lines=lines[-self.max_lines :],
            captured_at=datetime.now(UTC).isoformat(),
            expires_at=time.monotonic() + self.ttl,
            limited=len(lines) >= self.max_lines,
        )

    async def page(self, pane_id: str, limit: int, before: str | None) -> ScreenPage:
        self._expire()
        if before is None:
            snapshot = await self._capture(pane_id)
            key = self._store(snapshot)
            end = len(snapshot.lines)
        else:
            try:
                key, offset = before.rsplit(".", 1)
                end = int(offset)
            except ValueError as exc:
                raise HTTPException(400, "Invalid screen cursor") from exc
            snapshot = self.snapshots.get(key)
            if snapshot is None:
                raise HTTPException(
                    410, "Screen cursor expired; load the latest page again"
                )
            if snapshot.pane_id != pane_id or not 0 <= end <= len(snapshot.lines):
                raise HTTPException(
                    400, "Screen cursor does not match this pane or range"
                )
            self.snapshots.move_to_end(key)

        start = max(0, end - limit)
        return ScreenPage(
            pane_id=pane_id,
            lines=snapshot.lines[start:end],
            captured_at=snapshot.captured_at,
            next_before=f"{key}.{start}" if start else None,
            has_more=start > 0,
            history_limited=snapshot.limited,
        )
