import glob
import json
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field


def _find_claude_jsonl(session_id: str, cwd: str) -> str | None:
    if session_id:
        pattern = os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    if cwd:
        slug = "-" + cwd.strip("/").replace("/", "-")
        project_dir = os.path.expanduser(f"~/.claude/projects/{slug}")
        if os.path.isdir(project_dir):
            files = sorted(
                glob.glob(os.path.join(project_dir, "*.jsonl")),
                key=os.path.getmtime,
                reverse=True,
            )
            if files:
                return files[0]
    return None


def _parse_claude_jsonl(fpath: str) -> list[str]:
    lines: list[str] = []
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                t = obj.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                if t == "user" and isinstance(content, str):
                    txt = content.strip()
                    if (
                        not txt
                        or txt.startswith("<local-command")
                        or txt.startswith("<command-")
                    ):
                        continue
                    lines.append("")
                    for i, line in enumerate(txt.splitlines()):
                        lines.append(f"❯ {line}" if i == 0 else f"  {line}")
                elif t == "assistant" and isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            txt = (item.get("text") or "").strip()
                            if txt:
                                lines.append("")
                                for i, line in enumerate(txt.splitlines()):
                                    lines.append(f"● {line}" if i == 0 else f"  {line}")
                        elif item.get("type") == "tool_use":
                            name = item.get("name") or "Tool"
                            inp = item.get("input") or {}
                            summary = (
                                inp.get("command")
                                or inp.get("file_path")
                                or inp.get("pattern")
                                or ""
                            )
                            if isinstance(summary, str):
                                summary = summary.splitlines()[0][:80]
                            lines.append(f"● {name}({summary})")
    except Exception:
        return []
    return lines


def _find_codex_jsonl(
    session_id: str, cwd: str, live_lines: list[str]
) -> str | None:
    pattern = os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")
    try:
        all_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    except Exception:
        return None
    if not all_files:
        return None

    by_id = [
        f for f in all_files if session_id and session_id in os.path.basename(f)
    ]
    candidates: list[str] = []
    if cwd:
        for fpath in all_files[:40]:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for _ in range(12):
                        raw = f.readline()
                        if not raw:
                            break
                        obj = json.loads(raw)
                        p = obj.get("payload")
                        if isinstance(p, dict) and p.get("cwd") == cwd:
                            candidates.append(fpath)
                            break
            except Exception:
                continue

    prompts = [
        line.strip().lstrip("›").strip()
        for line in live_lines
        if line.strip().startswith("›") and len(line.strip()) > 3
    ]
    if prompts and candidates:
        for c in candidates:
            try:
                with open(c, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if any(p[:16] in text for p in prompts if p):
                    return c
            except Exception:
                continue

    if by_id:
        return by_id[0]
    return candidates[0] if candidates else None


def _parse_codex_jsonl(fpath: str) -> list[str]:
    lines: list[str] = []
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("type") != "response_item":
                    continue
                p = obj.get("payload") or {}
                if p.get("type") != "message":
                    continue
                role = p.get("role")
                content = p.get("content") or []
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    txt = (item.get("text") or "").strip()
                    if not txt:
                        continue
                    if role == "user":
                        if (
                            txt.startswith("<environment_context>")
                            or txt.startswith("# AGENTS.md")
                            or txt.startswith("<INSTRUCTIONS>")
                        ):
                            continue
                        lines.append("")
                        for i, line in enumerate(txt.splitlines()):
                            lines.append(f"› {line}" if i == 0 else f"  {line}")
                    elif role == "assistant":
                        lines.append("")
                        for i, line in enumerate(txt.splitlines()):
                            lines.append(f"• {line}" if i == 0 else f"  {line}")
    except Exception:
        return []
    return lines


def load_agent_history_fallback(
    agent: str,
    session_id: str,
    cwd: str,
    live_lines: list[str],
    max_lines: int = 2000,
) -> list[str]:
    """When a TUI agent (Codex / Claude Code in alternate-screen or resumed mode)
    only exposes the visible viewport rows via `otty pane capture`, load its
    earlier conversation turns from the local session JSONL file."""
    agent_lower = (agent or "").lower()
    history: list[str] = []
    if "claude" in agent_lower:
        fpath = _find_claude_jsonl(session_id, cwd)
        if fpath:
            history = _parse_claude_jsonl(fpath)
    elif "codex" in agent_lower:
        fpath = _find_codex_jsonl(session_id, cwd, live_lines)
        if fpath:
            history = _parse_codex_jsonl(fpath)
    if not history:
        return []

    first_prompts = [
        line.strip()
        for line in live_lines
        if line.strip().startswith(("› ", "❯ ", "● ", "• ")) and len(line.strip()) > 6
    ]
    if first_prompts:
        anchor = first_prompts[0][:24]
        for idx in range(len(history) - 1, max(-1, len(history) - 120), -1):
            if history[idx].strip().startswith(anchor[:16]):
                history = history[:idx]
                break

    while history and not history[-1].strip():
        history.pop()
    if not history:
        return []

    combined = history + ["", "─── 以上为历史会话记录 · 以下为当前终端实时画面 ───", ""]
    return combined[-max_lines:]


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

    async def _capture(
        self,
        pane_id: str,
        agent: str = "",
        agent_session_id: str = "",
        cwd: str = "",
    ) -> Snapshot:
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
        if len(lines) <= 80 and (agent or agent_session_id):
            fallback = load_agent_history_fallback(
                agent=agent,
                session_id=agent_session_id,
                cwd=cwd,
                live_lines=lines,
                max_lines=max(0, self.max_lines - len(lines)),
            )
            if fallback:
                lines = fallback + lines
        return Snapshot(
            pane_id=pane_id,
            lines=lines[-self.max_lines :],
            captured_at=datetime.now(UTC).isoformat(),
            expires_at=time.monotonic() + self.ttl,
            limited=len(lines) >= self.max_lines,
        )

    async def page(
        self,
        pane_id: str,
        limit: int,
        before: str | None,
        agent: str = "",
        agent_session_id: str = "",
        cwd: str = "",
    ) -> ScreenPage:
        self._expire()
        if before is None:
            snapshot = await self._capture(
                pane_id,
                agent=agent,
                agent_session_id=agent_session_id,
                cwd=cwd,
            )
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
