"""
Shared-secret auth dependency for routes that have no frontend usage and
would otherwise rely entirely on Cloudflare Access as their only gate.

Cloudflare Access only intercepts requests that resolve app.remediatelabs.io
through Cloudflare's own proxy — it does nothing for requests that connect
directly to the ALB's own AWS-assigned hostname instead (exactly what
.github/workflows/deploy.yml's own health check does on purpose, via
--connect-to, to bypass Cloudflare for deploy-time checks). Any route gated
only by Cloudflare Access is reachable by anyone who has that ALB hostname.

require_admin_token() closes that gap for the specific routes that don't
need to work from the browser dashboard (which can't use a static shared
secret without exposing it client-side) — /debug/* and /injection/trigger.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from app.core.config import settings


def require_admin_token(request: Request) -> None:
    """Reject the request unless ADMIN_API_TOKEN matches.

    Token can come from the `X-Admin-Token` header or the `?token=…` query
    string (for convenience in manual/curl use). Constant-time comparison
    prevents timing attacks.

    Fails closed: an unset ADMIN_API_TOKEN returns 503, not a silent pass —
    unlike cloudwatch_webhook_token, there's no scenario where it's
    acceptable for this check to be silently disabled.
    """
    expected = settings.admin_api_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_TOKEN not configured — this route is disabled until it is set.",
        )
    provided = request.headers.get("X-Admin-Token") or request.query_params.get("token", "")
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid admin token")
