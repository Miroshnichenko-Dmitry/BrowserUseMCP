"""Static bearer-token auth for HTTP transport (constant-time comparison).

Per-agent tokens put the agent name into the access token's claims; the browser endpoint uses
it to tell agents apart.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class BearerTokenVerifier(TokenVerifier):
    def __init__(self, tokens: dict[str, str | None]):
        """`tokens` maps each accepted token to its agent name (None: shared, anonymous token)."""
        if not tokens:
            raise ValueError("at least one token is required")
        super().__init__()
        self._tokens = [(token.encode(), name) for token, name in tokens.items()]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        matched: tuple[str | None] | None = None
        for expected, name in self._tokens:  # compare against all: timing does not reveal which
            if hmac.compare_digest(presented, expected):
                matched = (name,)
        if matched is None:
            return None
        (name,) = matched
        return AccessToken(
            token=token,
            client_id=name or "shared-token",
            scopes=[],
            claims={"agent": name} if name else {},
        )
