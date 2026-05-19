"""Tests for the OAuth state persistence backends.

Two backends:

- :class:`InMemoryTokenStore` — default, wipes on restart.
- :class:`RedisTokenStore` — exercised via ``fakeredis`` so we cover the
  exact code path used in production (Redis pipeline, EXPIRE TTL,
  Fernet encryption round-trip) without a real Redis server.

The provider-level integration tests live in ``test_auth.py``; this
file stays focused on the storage layer alone.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from pexels_mcp_server.storage import (
    InMemoryTokenStore,
    RedisTokenStore,
    build_token_store,
)

_CLIENT_ID = "client-test"
_TOKEN = "mcp_test_access_token"
_PEXELS_KEY = "test-pexels-key"


def _make_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=_CLIENT_ID,
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )


def _make_access_token() -> AccessToken:
    return AccessToken(
        token=_TOKEN,
        client_id=_CLIENT_ID,
        scopes=["mcp"],
        expires_at=2_000_000_000,
        resource="https://example.com",
    )


# --------------------------------------------------------- in-memory store


async def test_in_memory_roundtrip_for_client_token_and_key() -> None:
    store = InMemoryTokenStore()
    client = _make_client()
    token = _make_access_token()

    await store.set_client(client)
    assert (await store.get_client(_CLIENT_ID)) == client

    await store.set_access_token(token, ttl_seconds=3600)
    assert (await store.get_access_token(_TOKEN)) == token

    await store.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=3600)
    assert (await store.get_pexels_key(_TOKEN)) == _PEXELS_KEY


async def test_in_memory_delete_drops_token_and_key() -> None:
    store = InMemoryTokenStore()
    await store.set_access_token(_make_access_token(), ttl_seconds=3600)
    await store.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=3600)

    await store.delete_access_token(_TOKEN)
    await store.delete_pexels_key(_TOKEN)

    assert (await store.get_access_token(_TOKEN)) is None
    assert (await store.get_pexels_key(_TOKEN)) is None


async def test_in_memory_client_cap_evicts_oldest_half() -> None:
    store = InMemoryTokenStore(max_tracked_clients=4)
    for i in range(5):
        await store.set_client(
            OAuthClientInformationFull(
                client_id=f"c-{i}",
                redirect_uris=[AnyUrl("https://example.com/cb")],
            )
        )
    # The oldest two clients were evicted when the 5th was inserted.
    assert (await store.get_client("c-0")) is None
    assert (await store.get_client("c-1")) is None
    assert (await store.get_client("c-4")) is not None


async def test_in_memory_token_cap_evicts_oldest_tenth() -> None:
    store = InMemoryTokenStore(max_tracked_tokens=10)
    for i in range(11):
        await store.set_access_token(
            AccessToken(
                token=f"t-{i}",
                client_id=_CLIENT_ID,
                scopes=["mcp"],
                expires_at=2_000_000_000,
                resource=None,
            ),
            ttl_seconds=3600,
        )
        await store.set_pexels_key(f"t-{i}", f"k-{i}", ttl_seconds=3600)
    # The oldest 10 % (1 entry) is dropped; the freshest one stays.
    assert (await store.get_access_token("t-0")) is None
    assert (await store.get_pexels_key("t-0")) is None
    assert (await store.get_access_token("t-10")) is not None


# ----------------------------------------------------------------- Redis


@pytest.fixture
def fake_redis_store() -> RedisTokenStore:
    """Build a Redis store backed by fakeredis with a freshly minted key."""
    return RedisTokenStore(
        redis=FakeAsyncRedis(decode_responses=False),  # type: ignore[arg-type]
        encryption_key=Fernet.generate_key().decode("utf-8"),
    )


async def test_redis_roundtrip_for_client_token_and_key(
    fake_redis_store: RedisTokenStore,
) -> None:
    store = fake_redis_store
    client = _make_client()
    token = _make_access_token()

    await store.set_client(client)
    loaded_client = await store.get_client(_CLIENT_ID)
    assert loaded_client is not None
    assert loaded_client.client_id == client.client_id

    await store.set_access_token(token, ttl_seconds=3600)
    loaded_token = await store.get_access_token(_TOKEN)
    assert loaded_token is not None
    assert loaded_token.token == _TOKEN

    await store.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=3600)
    assert (await store.get_pexels_key(_TOKEN)) == _PEXELS_KEY


async def test_redis_pexels_key_is_encrypted_at_rest(
    fake_redis_store: RedisTokenStore,
) -> None:
    """Reading the raw Redis value must NOT yield the plaintext Pexels key.

    A leaked Redis snapshot alone (without ``MCP_ENCRYPTION_KEY``) is not
    enough to recover any user's Pexels API key — the Fernet ciphertext
    is opaque without the key."""
    store = fake_redis_store
    await store.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=3600)
    raw = await store._redis.get(f"mcp:pexels:{_TOKEN}")  # type: ignore[arg-type]
    assert raw is not None
    assert _PEXELS_KEY.encode("utf-8") not in raw, (
        "Pexels key must be encrypted in Redis, not stored as plaintext"
    )
    # The encrypted value still decrypts via the public API.
    assert (await store.get_pexels_key(_TOKEN)) == _PEXELS_KEY


async def test_redis_rotated_encryption_key_drops_stale_binding() -> None:
    """Rotating ``MCP_ENCRYPTION_KEY`` invalidates existing bindings gracefully.

    The user re-walks /setup on their next call instead of seeing a
    cryptic decryption error. Verified by writing with one key and
    reading with another."""
    redis = FakeAsyncRedis(decode_responses=False)
    writer = RedisTokenStore(redis=redis, encryption_key=Fernet.generate_key().decode())  # type: ignore[arg-type]
    await writer.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=3600)

    # The operator rotated MCP_ENCRYPTION_KEY: a fresh store with a new key
    # cannot decrypt the existing ciphertext.
    reader = RedisTokenStore(redis=redis, encryption_key=Fernet.generate_key().decode())  # type: ignore[arg-type]
    assert (await reader.get_pexels_key(_TOKEN)) is None
    # The stale entry is dropped so subsequent reads are short-circuited.
    raw = await redis.get(f"mcp:pexels:{_TOKEN}")
    assert raw is None


async def test_redis_ttl_is_applied_on_set(fake_redis_store: RedisTokenStore) -> None:
    """``set_access_token`` and ``set_pexels_key`` must apply the requested TTL."""
    store = fake_redis_store
    await store.set_access_token(_make_access_token(), ttl_seconds=120)
    await store.set_pexels_key(_TOKEN, _PEXELS_KEY, ttl_seconds=120)
    # fakeredis honours TTLs; querying TTL returns the remaining seconds.
    token_ttl = await store._redis.ttl(f"mcp:token:{_TOKEN}")  # type: ignore[arg-type]
    key_ttl = await store._redis.ttl(f"mcp:pexels:{_TOKEN}")  # type: ignore[arg-type]
    assert 0 < token_ttl <= 120
    assert 0 < key_ttl <= 120


# --------------------------------------------------------- build_token_store


def test_build_token_store_picks_in_memory_when_no_redis_url() -> None:
    store = build_token_store(redis_url=None, encryption_key=None)
    assert isinstance(store, InMemoryTokenStore)


def test_build_token_store_refuses_redis_without_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="MCP_ENCRYPTION_KEY"):
        build_token_store(redis_url="redis://localhost:6379", encryption_key=None)


def test_build_token_store_builds_redis_when_url_and_key_present() -> None:
    """A real ``Redis.from_url`` call doesn't connect synchronously, so we
    can verify the wiring without a live server."""
    store = build_token_store(
        redis_url="redis://localhost:6379",
        encryption_key=Fernet.generate_key().decode(),
    )
    assert isinstance(store, RedisTokenStore)


def test_build_token_store_rejects_malformed_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="32-byte url-safe base64"):
        build_token_store(
            redis_url="redis://localhost:6379",
            encryption_key="not-a-fernet-key",
        )
