"""run-speed-test waits for and reports the new result rows.

Live on fw 6.35.8 (2026-11-24): the POST answers HTTP 200 inline in ~2 s; the test then runs ~20 s and an
upstream row and a downstream row appear on speed.ha. The page keeps only the last 8 rows, so new rows are
recognised by (Time, Direction), never by the count.
"""

import json

from save_helpers import client_with, html

from bgwcli import cli

ROW = '<tr><td>{t}</td><td>{d}</td><td>{m}</td><td>28</td><td>{l}</td><td>Success</td></tr>'
OLD = [("11/23/2026 20:03:14", "upstream", "643.050000", "23.000000"),
       ("11/23/2026 20:01:56", "downstream", "648.960000", "87.000000")]
NEW = [("11/24/2026 14:13:01", "downstream", "649.310000", "27.000000"),
       ("11/24/2026 14:12:48", "upstream", "545.280000", "23.000000")]


def _page(rows):
    body = "".join(ROW.format(t=t, d=d, m=m, l=lat) for t, d, m, lat in rows)
    return (
        '<html><head><title>Speed Test</title></head><body><form method="post" action="/cgi-bin/speed.ha">'
        '<input type="hidden" name="nonce" value="abc123"><input type="submit" name="run" value="Run Speed Test">'
        "</form><table><tr><th>Time</th><th>Direction</th><th>Mbps</th><th>Server</th><th>Latency ms</th>"
        f"<th>Result</th></tr>{body}</table></body></html>"
    )


def _run(monkeypatch, capsys, after_post_pages, json_out=True):
    state = {"posted": False, "reads_after": 0}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(_page(OLD))
        if not state["posted"]:
            return html(_page(OLD))
        index = min(state["reads_after"], len(after_post_pages) - 1)
        state["reads_after"] += 1
        return html(after_post_pages[index])

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    argv = ["action", "run-speed-test", "--commit"] + (["--json"] if json_out else [])
    code = cli.main(argv)
    out = capsys.readouterr().out
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, out, posts, state


def test_the_new_rows_are_reported(monkeypatch, capsys, tmp_env, clock):
    pages = [_page(OLD), _page([NEW[1], *OLD]), _page([*NEW, *OLD])]  # running, half done, done
    code, out, posts, _ = _run(monkeypatch, capsys, pages)
    data = json.loads(out)
    assert code == 0 and len(posts) == 1, data
    assert data["committed"] is True
    rows = data["answer"]["tables"]
    assert {r["Direction"] for r in rows} == {"upstream", "downstream"}
    assert {r["Mbps"] for r in rows} == {"649.310000", "545.280000"}
    assert "649.31" in data["result"] and "545.28" in data["result"]


def test_new_rows_are_found_when_the_count_stays_at_eight(monkeypatch, capsys, tmp_env, clock):
    eight_old = [(f"11/2{i}/2026 10:00:0{i}", "upstream" if i % 2 else "downstream", "600.0", "20.0") for i in range(8)]
    rotated = [*NEW, *eight_old[:6]]  # still 8 rows: the 2 oldest dropped
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(_page(eight_old))
        return html(_page(rotated if state["posted"] else eight_old))

    client, _ = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", "run-speed-test", "--commit", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and len(data["answer"]["tables"]) == 2, data


def test_no_new_rows_in_time_is_exit_2(monkeypatch, capsys, tmp_env, clock):
    code, out, posts, _ = _run(monkeypatch, capsys, [_page(OLD)])
    data = json.loads(out)
    assert code == 2 and len(posts) == 1
    assert data["committed"] is False and "speed test may still be running" in data["warning"]


def test_text_output_prints_the_result(monkeypatch, capsys, tmp_env, clock):
    code, out, _, _ = _run(monkeypatch, capsys, [_page([*NEW, *OLD])], json_out=False)
    assert code == 0 and "649.31" in out and "545.28" in out


def test_the_page_is_never_polled_forever(monkeypatch, capsys, tmp_env, clock):
    _, _, _, state = _run(monkeypatch, capsys, [_page(OLD)])
    assert state["reads_after"] <= 25


def test_a_page_without_history_reports_as_before(monkeypatch, capsys, tmp_env, clock):
    empty = (
        '<html><head><title>Speed Test</title></head><body><form method="post" action="/cgi-bin/speed.ha">'
        '<input type="hidden" name="nonce" value="abc123"><input type="submit" name="run" value="Run Speed Test">'
        "</form></body></html>"
    )
    client, wire = client_with(lambda request, n: html(empty))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", "run-speed-test", "--commit", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["committed"] is True and "answer" not in data
    post = next(i for i, r in enumerate(wire.requests) if r.method == "POST" and "/login.ha" not in r.url)
    assert not [r for r in wire.requests[post + 1 :] if r.method == "GET"], "nothing to compare with: no polling"
