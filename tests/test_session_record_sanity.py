"""Cache and cooldown records with non-finite or mis-typed fields hold nothing; imported cookies are checked."""

from __future__ import annotations

import json

import pytest
from save_helpers import FakeTransport

from bgwcli import session
from bgwcli.client import BGW320Client
from bgwcli.session import SessionCoordinatorOptions, with_router_session

ORIGIN = "http://router.local"
OPTIONS = SessionCoordinatorOptions(
    cache_ttl_ms=120000, pool_cooldown_ms=300000, lock_timeout_ms=1000, wait_for_session=False,
)


def _client() -> BGW320Client:
    return BGW320Client(ORIGIN, access_code="12345", timeout_ms=1000, user_agent="test",
                        transport=FakeTransport(lambda request, n: None))


def _write(path, text: str) -> None:
    session._ensure_private_dir(path.parent)
    path.write_text(text)
    path.chmod(0o600)


NOW = "9999999999999"
HEAD = '{"origin":"http://router.local","authenticated":true,'
BAD_CACHES = [
    HEAD + '"cookies":{"sid":"a"},"cachedAt":1,"expiresAt":Infinity}',
    HEAD + '"cookies":{"sid":"a"},"cachedAt":NaN,"expiresAt":' + NOW + '}',
    HEAD + '"cookies":{"sid":"a"},"expiresAt":"soon"}',
    HEAD + '"cookies":["sid","a"],"expiresAt":' + NOW + '}',
    HEAD + '"cookies":{"sid":7},"expiresAt":' + NOW + '}',
]
BAD_COOLDOWNS = [
    '{"until":Infinity}', '{"until":NaN,"waitedMs":1}', '{"until":"later"}',
    '{"until":' + NOW + ',"waitedMs":Infinity}', '{"until":' + NOW + ',"retryCount":"many"}',
]


@pytest.mark.parametrize("text", BAD_CACHES)
def test_a_cache_with_unusable_fields_holds_nothing_and_is_replaced(tmp_env, text):
    paths = session.session_paths(ORIGIN)
    _write(paths.cache, text)
    state = session.read_session_state(ORIGIN, cache_ttl_ms=120000, pool_cooldown_ms=300000)
    assert state.cached is False
    client = _client()
    with_router_session(client, OPTIONS, lambda: None)
    assert client.has_authenticated_session() is False
    assert not paths.cache.exists()


@pytest.mark.parametrize("text", BAD_COOLDOWNS)
def test_a_cooldown_with_unusable_fields_blocks_nothing_and_is_removed(tmp_env, text):
    paths = session.session_paths(ORIGIN)
    _write(paths.cooldown, text)
    assert session.read_session_state(ORIGIN, cache_ttl_ms=120000, pool_cooldown_ms=300000).pool_cooldown_until is None
    assert with_router_session(_client(), OPTIONS, lambda: "ran") == "ran"
    assert not paths.cooldown.exists()


def test_a_sound_record_still_imports(tmp_env):
    paths = session.session_paths(ORIGIN)
    now = session._now_ms()
    _write(paths.cache, json.dumps({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "a"},
                                    "cachedAt": now, "expiresAt": now + 60000}))
    client = _client()
    with_router_session(client, OPTIONS, lambda: None)
    assert client.has_authenticated_session() is True


def test_import_session_drops_mistyped_and_control_character_cookies():
    client = _client()
    client.import_session({"origin": ORIGIN, "authenticated": True,
                           "cookies": {"sid": "ok", "bad": "a\r\nSet-Cookie: x=1", "n": 5, 7: "x", "nul": "a\x00b"}})
    assert client.export_session().cookies == {"sid": "ok"}
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": ["sid", "ok"]})
    assert client.has_authenticated_session() is False
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": {"x": "a\x7f"}})
    assert client.has_authenticated_session() is False


def test_set_cookie_values_with_control_characters_are_dropped():
    client = _client()
    client._store_cookies(["sid=ok; Path=/", "bad=a\x01b", "worse\x7f=1"])
    assert dict(client._cookies) == {"sid": "ok"}


def test_clear_cache_refuses_to_delete_a_foreign_owned_record(tmp_env, monkeypatch):
    from types import SimpleNamespace

    from bgwcli.errors import SessionLockError

    paths = session.session_paths(ORIGIN)
    _write(paths.cache, "{}")
    _write(paths.cooldown, "{}")
    real = session._require_own_file
    # The lock marker and directory are ours; only the records look foreign to this process.
    monkeypatch.setattr(session, "_require_own_file",
                        lambda path, info: real(path, SimpleNamespace(st_uid=info.st_uid + 1)))
    with pytest.raises(SessionLockError, match="owned by another user"):
        session.clear_session_state(ORIGIN, 1000)
    assert paths.cache.exists() and paths.cooldown.exists()
