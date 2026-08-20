"""Multi-user hosting support: map caller identity to a per-user project root.

Hosted mode is opt-in via the `SAARI_DATA_ROOT` env var. When set, the ASGI
middleware below resolves every request to a project directory:

    $SAARI_DATA_ROOT/<user-id>/<project-name>/

- user id comes from `x-ms-client-principal-id` (what Azure Container Apps /
  App Service "Easy Auth" injects after validating the Entra ID token) or,
  for other deployments, `x-saari-user`.
- project name comes from the `x-saari-project` header (default: "default").

The platform in front of us is the authenticator; these headers are trusted
because the ingress strips client-supplied copies and injects its own. Do
not expose a hosted saari without such a layer.

Locally (`SAARI_DATA_ROOT` unset) none of this runs and root resolution
stays cwd/env-based. The same middleware wraps both the FastAPI app
(server.py) and the streamable-HTTP MCP app (mcp_server.py), so every
surface scopes the same way.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from saari import paths

USER_HEADERS = ("x-ms-client-principal-id", "x-saari-user")
PROJECT_HEADER = "x-saari-project"
DEFAULT_PROJECT = "default"

# Path components derived from headers must never traverse. Entra object ids
# are GUIDs; project names are user-chosen, so both are validated.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def data_root() -> Path | None:
    """The multi-user data root, or None when not in hosted mode."""
    env = os.environ.get("SAARI_DATA_ROOT")
    return Path(env).expanduser().resolve() if env else None


def _safe(component: str) -> str | None:
    component = component.strip()
    if not _SAFE_COMPONENT.match(component) or ".." in component:
        return None
    return component


def resolve_hosted_root(headers: dict[str, str]) -> Path | None:
    """Resolve (and lazily initialize) the caller's project directory.

    Returns None when not in hosted mode or when no user identity header is
    present. Raises ValueError on malformed user/project components.
    """
    base = data_root()
    if base is None:
        return None

    user_raw = next((headers[h] for h in USER_HEADERS if headers.get(h)), None)
    if user_raw is None:
        return None
    user = _safe(user_raw)
    project = _safe(headers.get(PROJECT_HEADER) or DEFAULT_PROJECT)
    if user is None or project is None:
        raise ValueError("invalid user or project identifier")

    root = base / user / project
    if not (root / paths.MARKER).is_dir():
        paths.init_project(root)
    return root


def list_projects(user_root: Path) -> list[str]:
    """Names of the user's projects (directories containing `.saaristo/`)."""
    if not user_root.is_dir():
        return []
    return sorted(
        p.name for p in user_root.iterdir() if (p / paths.MARKER).is_dir()
    )


class RequestRootMiddleware:
    """Pure-ASGI middleware: bind the request-scoped project root.

    Framework-agnostic so the FastAPI app and the MCP streamable-HTTP app
    share it. No-op for every request when hosted mode is off.
    """

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    # Reachable without identity: platform health probes bypass auth.
    EXEMPT_PATHS = frozenset({"/api/health"})

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if (
            scope["type"] != "http"
            or data_root() is None
            or scope.get("path") in self.EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        try:
            root = resolve_hosted_root(headers)
        except ValueError:
            await _plain_response(send, 400, b"invalid user or project identifier")
            return
        if root is None:
            await _plain_response(send, 401, b"missing identity header")
            return

        token = paths.set_request_root(root)
        try:
            await self.app(scope, receive, send)
        finally:
            paths.reset_request_root(token)


async def _plain_response(send, status: int, body: bytes) -> None:  # type: ignore[no-untyped-def]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})
