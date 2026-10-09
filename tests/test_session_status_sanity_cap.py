"""`session status` applies the same wall-clock sanity cap as the coordinator: a cooldown stamped
further ahead than one configured cooldown length is expired, not reported as active."""

from __future__ import annotations

import json
import time

from bgwcli import cli, session


def test_session_status_ignores_a_cooldown_stamped_beyond_the_configured_length(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BGW_SESSION_POOL_COOLDOWN_MS", "1000")
    host = "http://router.local"
    origin = cli.router_session_identity(host)
    paths = session.session_paths(origin)
    paths.cooldown.parent.mkdir(parents=True, exist_ok=True)
    paths.cooldown.write_text(json.dumps({"until": int(time.time() * 1000) + 3_600_000}))
    code = cli.main(["session", "status", "--host", host, "--json"])
    state = json.loads(capsys.readouterr().out)
    assert code == 0 and not state.get("poolCooldownUntil")
