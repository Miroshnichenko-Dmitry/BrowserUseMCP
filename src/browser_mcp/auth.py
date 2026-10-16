"""Static bearer-token auth for HTTP transport (constant-time comparison)."""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class BearerTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        if len(token) < 16:
            raise ValueError("AUTH_TOKEN must be at least 16 characters")
        super().__init__()
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="static-token", scopes=[])
        return None
