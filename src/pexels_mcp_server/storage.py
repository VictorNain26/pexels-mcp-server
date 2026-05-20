"""Persistent backing store for the OAuth provider state.

The provider's hot state — registered DCR clients, issued access tokens,
and the per-user Pexels API key bound to each token — historically lived
in process-local dicts (see :class:`_InMemoryTokenStore`). That meant
every server restart wiped every user's session and forced them to
re-walk the OAuth handshake + re-paste their Pexels key.

When the operator sets ``REDIS_URL`` (Upstash Redis serverless, Redis
Cloud, or any TLS Redis endpoint), the provider switches to
:class:`RedisTokenStore` and the same state survives restarts. The
Pexels API key is **encrypted at rest** with Fernet (AES-128-CBC + HMAC-
SHA256) using a separate ``MCP_ENCRYPTION_KEY`` env var — a leaked Redis
snapshot alone does not yield the keys.

Transient state (auth codes, pending /setup sessions, the
code-to-key transitional binding) stays in process memory regardless of
backend: their TTLs are 5 / 15 minutes and they don't survive any user
hiccup anyway. Persisting them adds round-trips for zero benefit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger("pexels_mcp_server.storage")


_CLIENT_PREFIX = "mcp:client:"
_TOKEN_PREFIX = "mcp:token:"
_PEXELS_KEY_PREFIX = "mcp:pexels:"


class TokenStore(Protocol):
    """Async persistence Protocol for the OAuth provider state.

    All methods are async to allow Redis (or any other async backend) to
    plug in without changing call sites. The in-memory implementation
    completes each call synchronously inside an ``async def`` shim.
    """

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...

    async def set_client(self, client: OAuthClientInformationFull) -> None: ...

    async def get_access_token(self, token: str) -> AccessToken | None: ...

    async def set_access_token(self, access_token: AccessToken, ttl_seconds: int) -> None: ...

    async def delete_access_token(self, token: str) -> None: ...

    async def get_pexels_key(self, token: str) -> str | None: ...

    async def set_pexels_key(self, token: str, pexels_key: str, ttl_seconds: int) -> None: ...

    async def delete_pexels_key(self, token: str) -> None: ...

    async def aclose(self) -> None: ...


# ============================================================ in-memory


class InMemoryTokenStore:
    """Process-local store. Wipes on restart, no external deps.

    Default backend (used by stdio transport, tests, and the HTTP
    transport when ``REDIS_URL`` is unset). The FIFO eviction caps on
    ``_clients`` and ``_tokens`` bound memory under sustained traffic;
    the public ``PexelsOAuthProvider`` still runs its periodic sweep to
    drop expired entries.
    """

    def __init__(
        self,
        *,
        max_tracked_clients: int = 10_000,
        max_tracked_tokens: int = 10_000,
    ) -> None:
        self._max_tracked_clients = max_tracked_clients
        self._max_tracked_tokens = max_tracked_tokens
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._tokens: dict[str, AccessToken] = {}
        self._token_to_key: dict[str, str] = {}

    # ------------------------------------------------------------------ DCR

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def set_client(self, client: OAuthClientInformationFull) -> None:
        client_id = client.client_id
        if not client_id:
            raise ValueError("client.client_id is required")
        if client_id not in self._clients and len(self._clients) >= self._max_tracked_clients:
            drop_count = self._max_tracked_clients // 2
            for stale_id in list(self._clients.keys())[:drop_count]:
                del self._clients[stale_id]
            logger.warning(
                "OAuth client store hit cap (%d), evicted oldest %d entries",
                self._max_tracked_clients,
                drop_count,
            )
        self._clients[client_id] = client

    # ---------------------------------------------------------- access tokens

    async def get_access_token(self, token: str) -> AccessToken | None:
        return self._tokens.get(token)

    async def set_access_token(self, access_token: AccessToken, ttl_seconds: int) -> None:
        del ttl_seconds  # in-memory: expiry is read off the AccessToken itself
        if access_token.token not in self._tokens and len(self._tokens) >= self._max_tracked_tokens:
            drop_count = max(1, self._max_tracked_tokens // 10)
            for stale_token in list(self._tokens.keys())[:drop_count]:
                del self._tokens[stale_token]
                self._token_to_key.pop(stale_token, None)
            logger.warning(
                "OAuth token store hit cap (%d), evicted oldest %d entries",
                self._max_tracked_tokens,
                drop_count,
            )
        self._tokens[access_token.token] = access_token

    async def delete_access_token(self, token: str) -> None:
        self._tokens.pop(token, None)

    # ----------------------------------------------------- bound Pexels keys

    async def get_pexels_key(self, token: str) -> str | None:
        return self._token_to_key.get(token)

    async def set_pexels_key(self, token: str, pexels_key: str, ttl_seconds: int) -> None:
        del ttl_seconds
        self._token_to_key[token] = pexels_key

    async def delete_pexels_key(self, token: str) -> None:
        self._token_to_key.pop(token, None)

    async def aclose(self) -> None:
        return None


# ============================================================ Redis


def _encryption_layer(encryption_key: str) -> object:
    """Build a Fernet instance from the base64 key string.

    Imports ``cryptography`` lazily so a stdio install (no
    ``REDIS_URL``) doesn't pay the import cost of a 5 MB native lib.
    """
    from cryptography.fernet import Fernet

    raw = encryption_key.strip().encode("utf-8")
    try:
        return Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "MCP_ENCRYPTION_KEY must be a 32-byte url-safe base64 string. "
            "Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


class RedisTokenStore:
    """Persistent store backed by Redis (Upstash / Redis Cloud / self-hosted).

    Keys are namespaced under ``mcp:client:``, ``mcp:token:``, and
    ``mcp:pexels:``. Access tokens and Pexels keys carry the access
    token's TTL via ``EXPIRE``; DCR client metadata has no TTL (claude.ai
    expects its registered ``client_id`` to keep working).

    The Pexels key is **encrypted client-side** with Fernet (AES-128-CBC
    + HMAC-SHA256) before being stored. A leaked Redis snapshot alone is
    not enough to recover any Pexels API key — the operator's
    ``MCP_ENCRYPTION_KEY`` env var is the second factor. Rotating that
    key invalidates every stored Pexels binding (graceful: users
    transparently re-walk /setup on their next call).
    """

    def __init__(
        self,
        *,
        redis: Redis,
        encryption_key: str,
    ) -> None:
        self._redis: Redis = redis
        self._fernet = _encryption_layer(encryption_key)

    # ------------------------------------------------------------------ DCR

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self._redis.get(f"{_CLIENT_PREFIX}{client_id}")
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def set_client(self, client: OAuthClientInformationFull) -> None:
        await self._redis.set(
            f"{_CLIENT_PREFIX}{client.client_id}",
            client.model_dump_json(),
        )

    # ---------------------------------------------------------- access tokens

    async def get_access_token(self, token: str) -> AccessToken | None:
        raw = await self._redis.get(f"{_TOKEN_PREFIX}{token}")
        if raw is None:
            return None
        return AccessToken.model_validate_json(raw)

    async def set_access_token(self, access_token: AccessToken, ttl_seconds: int) -> None:
        await self._redis.set(
            f"{_TOKEN_PREFIX}{access_token.token}",
            access_token.model_dump_json(),
            ex=ttl_seconds,
        )

    async def delete_access_token(self, token: str) -> None:
        await self._redis.delete(f"{_TOKEN_PREFIX}{token}")

    # ----------------------------------------------------- bound Pexels keys

    async def get_pexels_key(self, token: str) -> str | None:
        raw = await self._redis.get(f"{_PEXELS_KEY_PREFIX}{token}")
        if raw is None:
            return None
        try:
            plaintext = self._fernet.decrypt(raw)  # type: ignore[attr-defined]
        except Exception:
            # Decryption failure means the operator rotated
            # MCP_ENCRYPTION_KEY since the binding was written. Drop the
            # stale entry; the user re-walks /setup on the next call.
            logger.warning("Failed to decrypt Pexels key for token; dropping stale binding")
            await self.delete_pexels_key(token)
            return None
        return str(plaintext.decode("utf-8"))

    async def set_pexels_key(self, token: str, pexels_key: str, ttl_seconds: int) -> None:
        ciphertext: bytes = self._fernet.encrypt(  # type: ignore[attr-defined]
            pexels_key.encode("utf-8")
        )
        await self._redis.set(
            f"{_PEXELS_KEY_PREFIX}{token}",
            ciphertext,
            ex=ttl_seconds,
        )

    async def delete_pexels_key(self, token: str) -> None:
        await self._redis.delete(f"{_PEXELS_KEY_PREFIX}{token}")

    async def aclose(self) -> None:
        await self._redis.aclose()


def build_token_store(
    *,
    redis_url: str | None,
    encryption_key: str | None,
) -> TokenStore:
    """Resolve the persistence backend from the environment.

    Returns :class:`RedisTokenStore` when ``redis_url`` is set (and
    ``encryption_key`` must be set too — refuse to boot otherwise);
    otherwise returns :class:`InMemoryTokenStore`. The operator picks
    the backend implicitly via env vars rather than an explicit flag,
    matching the stdio/streamable-http pattern elsewhere in the server.
    """
    if not redis_url:
        return InMemoryTokenStore()
    if not encryption_key:
        raise RuntimeError(
            "REDIS_URL is set but MCP_ENCRYPTION_KEY is missing. "
            "Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" '
            "and set it as a server env var."
        )
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=False)
    logger.info("Token store: Redis at %s", _redact_url(redis_url))
    return RedisTokenStore(redis=client, encryption_key=encryption_key)


def _redact_url(url: str) -> str:
    """Strip the password from a redis:// URL for logging."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username:
            netloc = f"{parsed.username}:***@{netloc}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url
