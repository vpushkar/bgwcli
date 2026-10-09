"""Structured pool-full results retain their diagnostic evidence in coordination."""

import json
from types import SimpleNamespace

import pytest

from bgwcli import session
from bgwcli.client import BGW320Client

ORIGIN = "http://router.invalid"


@pytest.mark.parametrize(
    "result",
    [
        {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7},
        {"session_pool_full": True, "waited_ms": 4321, "retry_count": 7},
        SimpleNamespace(session_pool_full=True, waited_ms=4321, retry_count=7),
        ["ignored", ({"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7},)],
        [
            {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7},
            {"sessionPoolFull": True, "waitedMs": 1000, "retryCount": 20},
        ],
        [
            {"sessionPoolFull": True},
            SimpleNamespace(session_pool_full=True, waited_ms=4321, retry_count=7),
            {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7},
        ],
    ],
)
def test_result_metadata_survives_cooldown_and_subsequent_refusal(tmp_env, result):
    client = BGW320Client(ORIGIN, transport=lambda request: pytest.fail("no network expected"))
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "synthetic"}})
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    assert session.with_router_session(client, opts, lambda: result) is result
    paths = session.session_paths(ORIGIN)
    cooldown = json.loads(paths.cooldown.read_text())
    assert cooldown["waitedMs"] == 4321
    assert cooldown["retryCount"] == 7
    assert not client.has_authenticated_session()
    assert not paths.cache.exists()
    with pytest.raises(session.RouterSessionPoolFullError) as caught:
        session.with_router_session(client, opts, lambda: pytest.fail("cooldown must refuse the run"))
    assert caught.value.waited_ms == 4321
    assert caught.value.retry_count == 7
