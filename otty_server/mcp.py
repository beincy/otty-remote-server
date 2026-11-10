"""Local MCP group routes and the host's independent permission ceiling."""

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .app import TRUTHY

if TYPE_CHECKING:
    from .tunnel import InstanceCredentials

PERMISSIONS = {"read", "control", "manage"}


def mcp_permission(method: str, path: str) -> str | None:
    """Only the basic Otty endpoints can be reached from MCP."""
    import re

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


def validate_groups(groups: object) -> dict:
    if not isinstance(groups, dict) or len(groups) > 100:
        raise ValueError("MCP groups must be an object with at most 100 keys")
    for token, config in groups.items():
        if (
            not isinstance(token, str)
            or not 16 <= len(token) <= 512
            or token.strip() != token
        ):
            raise ValueError("MCP group keys must be 16-512 characters")
        if not isinstance(config, dict):
            raise TypeError("Invalid MCP group configuration")
        stamp = config.get("timestamp")
        permissions = config.get("permissions")
        members = config.get("members")
        if type(stamp) is not int or not 0 <= stamp <= 9007199254740991:
            raise ValueError(
                "MCP group timestamp must be a nonnegative integer (milliseconds)"
            )
        if (
            not isinstance(permissions, list)
            or "read" not in permissions
            or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions)
        ):
            raise ValueError(
                "MCP group permissions must include read, with optional control,manage"
            )
        if (
            not isinstance(members, list)
            or len(members) > 100
            or any(
                not isinstance(token, str)
                or not 16 <= len(token) <= 512
                or token.strip() != token
                for token in members
            )
        ):
            raise ValueError("MCP group members must be a list of instance tokens")
    return groups


class McpRoutes:
    def __init__(self, instance: InstanceCredentials):
        self.enabled = os.getenv("OTTY_MCP_ENABLED", "1").strip().lower() in TRUTHY
        self.permissions = tuple(
            dict.fromkeys(
                p.strip()
                for p in os.getenv("OTTY_MCP_PERMISSIONS", "read,control,manage").split(
                    ","
                )
            )
        )
        if "read" not in self.permissions or set(self.permissions) - PERMISSIONS:
            raise ValueError(
                "OTTY_MCP_PERMISSIONS must include read, with optional control,manage"
            )
        instance_file = (
            instance.path
            or Path(
                os.getenv(
                    "OTTY_SERVER_CREDENTIALS_FILE",
                    str(Path.home() / ".config/otty-server/instance.json"),
                )
            ).expanduser()
        )
        self.path = Path(
            os.getenv(
                "OTTY_MCP_ROUTES_FILE", str(instance_file.parent / "mcp-groups.json")
            )
        ).expanduser()
        if self.path.exists():
            self.reload()
        else:
            # Reuse the one existing instance token for the default one-host group.
            # No additional local MCP secret is generated.
            self.groups = {
                instance.token: {
                    "timestamp": 0,
                    "permissions": ["read", "control", "manage"],
                    "members": [instance.token],
                }
            }
            self.save()

    def reload(self):
        self.groups = validate_groups(
            json.loads(self.path.read_text(encoding="utf-8"))["groups"]
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".mcp-groups-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"groups": self.groups}, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def merge(self, groups: object) -> bool:
        incoming = validate_groups(groups)
        # Respect newer edits on disk, and only accept subscribed keys.
        self.reload()
        changed = False
        for token, group in incoming.items():
            if (
                token in self.groups
                and group["timestamp"] > self.groups[token]["timestamp"]
            ):
                self.groups[token] = group
                changed = True
        if changed:
            self.save()
        return changed

    def registration(self) -> dict:
        return {
            "type": "mcp.configure",
            "version": 2,
            "enabled": self.enabled,
            "permissions": list(self.permissions),
            "groups": self.groups,
        }

    def allows(self, method: str, path: str) -> bool:
        permission = mcp_permission(method, path)
        return (
            self.enabled and permission is not None and permission in self.permissions
        )
