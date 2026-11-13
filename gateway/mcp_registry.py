"""Persistent MCP group registry for the Python gateway.

Mirrors ``gateway-cf/mcp-registry.js``: local Otty Servers upload their group
route tables over the existing agent tunnel, the gateway keeps only the newest
timestamp per group key, persists them across restarts, and pushes changes to
every instance that subscribed to a changed key.

The registry itself is transport-agnostic (no FastAPI imports) so it can be
exercised directly in tests; ``gateway.app`` supplies the notify callback that
delivers ``mcp.groups.updated`` frames to connected agent tunnels.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

PERMISSIONS = {"read", "control", "manage"}
# Uploads are permission-gated tokens, not free-form strings.
_TOKEN_RE = re.compile(r"^\S+$")
MAX_GROUPS = 100
MAX_MEMBERS = 100
MAX_TOKEN_LENGTH = 512

NotifyFn = Callable[[str, str, dict[str, Any]], Awaitable[None]]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mcp_permission(method: str, path: str) -> str | None:
    """Permission class of an API call, or None when MCP may not reach it."""
    if method == "GET" and path == "/api/v1/health":
        return "read"
    if path == "/api/v1/tabs":
        return {"GET": "read", "POST": "manage"}.get(method)
    match = re.fullmatch(
        r"/api/v1/tabs/t_[A-Za-z0-9_-]+(?:/(screen|wait|messages|keys))?", path
    )
    if not match:
        return None
    suffix = match.group(1)
    if method == "GET" and suffix in {None, "screen", "wait"}:
        return "read"
    if method == "DELETE" and suffix is None:
        return "manage"
    if method == "POST" and suffix in {"messages", "keys"}:
        return "control"
    return None


def _validate_token(token: object, what: str) -> str:
    if (
        not isinstance(token, str)
        or not 16 <= len(token) <= MAX_TOKEN_LENGTH
        or not _TOKEN_RE.match(token)
    ):
        raise ValueError(f"MCP {what} must be 16-{MAX_TOKEN_LENGTH} characters")
    return token


def validate_groups(groups: object) -> dict[str, dict[str, Any]]:
    """Validate an uploaded route table; raise ValueError on anything unsafe."""
    if not isinstance(groups, dict) or len(groups) > MAX_GROUPS:
        raise ValueError(f"MCP groups must be an object with at most {MAX_GROUPS} keys")
    for token, config in groups.items():
        _validate_token(token, "group key")
        if not isinstance(config, dict):
            raise TypeError("Invalid MCP group configuration")
        stamp = config.get("timestamp")
        if type(stamp) is not int or not 0 <= stamp <= 9007199254740991:
            raise ValueError(
                "MCP group timestamp must be a nonnegative integer (milliseconds)"
            )
        perms = config.get("permissions")
        if (
            not isinstance(perms, list)
            or not perms
            or "read" not in perms
            or any(p not in PERMISSIONS for p in perms)
        ):
            raise ValueError(
                "MCP group permissions must include read, with optional control,manage"
            )
        members = config.get("members")
        if (
            not isinstance(members, list)
            or len(members) > MAX_MEMBERS
            or any(
                not isinstance(m, str)
                or not 16 <= len(m) <= MAX_TOKEN_LENGTH
                or not _TOKEN_RE.match(m)
                for m in members
            )
        ):
            raise ValueError("MCP group members must be a list of instance tokens")
    return groups


class McpRegistry:
    """Group routes + subscriptions + write receipts, persisted as one JSON file.

    Passing ``store_path=None`` keeps everything in memory (useful for tests);
    the default deployment sets ``OTTY_GATEWAY_MCP_STORE`` so routes survive
    gateway restarts like the Workers Durable Object storage does.
    """

    def __init__(self, store_path: Path | None = None):
        self.store_path = store_path
        # key_hash -> {"key": group key, "config": validated group config}
        self.groups: dict[str, dict[str, Any]] = {}
        # instance_hash -> {"connection_id": str, "keys": [group key hashes]}
        self.subs: dict[str, dict[str, Any]] = {}
        # receipt key -> {"fingerprint", "expires_at", "result"?}
        self.receipts: dict[str, dict[str, Any]] = {}
        if store_path is not None and store_path.exists():
            try:
                data = json.loads(store_path.read_text(encoding="utf-8"))
                if isinstance(data.get("groups"), dict):
                    self.groups = data["groups"]
                if isinstance(data.get("subs"), dict):
                    self.subs = data["subs"]
                if isinstance(data.get("receipts"), dict):
                    self.receipts = data["receipts"]
            except OSError, ValueError:
                # Corrupt state must not wedge the gateway; start over.
                self.groups, self.subs, self.receipts = {}, {}, {}

    def save(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".gateway-mcp-", dir=self.store_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "groups": self.groups,
                        "subs": self.subs,
                        "receipts": self.receipts,
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.chmod(name, 0o600)
            os.replace(name, self.store_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def group(self, key_hash: str) -> dict[str, Any] | None:
        entry = self.groups.get(key_hash)
        return entry["config"] if entry else None

    async def sync(
        self, instance_hash: str, connection_id: str, groups: object, notify: NotifyFn
    ) -> dict[str, dict[str, Any]]:
        """Merge an uploaded table by timestamp; notify subscribers of changes.

        Returns the latest config for every key in this upload, so the
        uploading server can immediately converge on a newer stored version.
        """
        incoming = validate_groups(groups)
        changed: dict[str, dict[str, Any]] = {}
        latest: dict[str, dict[str, Any]] = {}
        for key, config in incoming.items():
            key_hash = digest(key)
            previous = self.groups.get(key_hash)
            if (
                previous is None
                or config["timestamp"] > previous["config"]["timestamp"]
            ):
                self.groups[key_hash] = {"key": key, "config": config}
                latest[key] = config
                changed[key] = config
            else:
                latest[key] = previous["config"]
        self.subs[instance_hash] = {
            "connection_id": connection_id,
            "keys": sorted(digest(key) for key in incoming),
        }
        self.save()
        if changed:
            await self._broadcast(changed, notify)
        return latest

    async def _broadcast(self, changed: dict[str, dict[str, Any]], notify: NotifyFn):
        for instance_hash, sub in list(self.subs.items()):
            updates = {
                key: config
                for key, config in changed.items()
                if digest(key) in sub["keys"]
            }
            if updates:
                await notify(instance_hash, sub["connection_id"], updates)

    async def unsubscribe(self, instance_hash: str, connection_id: str) -> None:
        sub = self.subs.get(instance_hash)
        if sub is not None and sub["connection_id"] == connection_id:
            del self.subs[instance_hash]
            self.save()
