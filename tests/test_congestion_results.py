"""Wi-Fi congestion detection reports its results.

Live on fw 6.35.8 (2026-11-24): the Congestion / CongRadio2 POST on lanstatistics.ha answers
302 -> /cgi-bin/statspost.ha?N; requesting that page runs the detection (60-85 s) and redirects back
to lanstatistics.ha, whose `<div id="cong">` then holds a "Time: ..." row and a Channel / AP Count /
Congestion Score table. The results are kept per web session: another session sees the section
without a table. bgwcli read that page only for a rejection banner and printed nothing.
"""

import json
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.congestion import parse_congestion

FORM_HEAD = (
    '<html><head><title>Home Network Status</title></head><body>'
    '<form method="post" action="/cgi-bin/lanstatistics.ha"><input type="hidden" name="nonce" value="abc123">'
    '<h2>Wi-Fi Congestion</h2><div id="cong">'
    '<p class="desc">WARNING: Running the Congestion test will temporarily disconnect Wi-Fi users.</p>'
    '<input type="submit" name="Congestion" class="cssbtn" value="Congestion Detection 2.4 GHz">'
    '<input type="submit" name="CongRadio2" class="cssbtn" value="Congestion Detection 5 GHz">'
)
FORM_TAIL = "</div></form></body></html>"


def _results(when: datetime) -> str:
    stamp = when.strftime("%a %b %d %H:%M:%S %Y")
    return (
        '<table cellpadding="1" class="table100" summary="Congestion Detection Results Table">'
        f"<tbody><tr><td>Time: {stamp}</td></tr>"
        '<tr><th scope="col">Channel</th><th scope="col">AP Count</th><th scope="col">Congestion Score</th></tr>'
        '<tr><td scope="row">36</td><td>13</td><td>8</td></tr>'
        '<tr><td scope="row">40</td><td>2</td><td>10</td></tr>'
        "</tbody></table>"
    )


EMPTY = FORM_HEAD + FORM_TAIL
REFUSED = (
    '<img id="error-message-icon" src="/images/icon_error.png"><div id="error-message-text">Congestion detection '
    "cannot be run because you are currently using a Wi-Fi DFS channel.</div>" + EMPTY
)
SCAN = ["action", "detect-wifi-congestion-5", "--commit", "--confirm", "CONGESTION-5"]


def _run(monkeypatch, capsys, after_page, *, json_out=True):
    state = {"posted": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/statspost.ha?22"})
        if path == "/cgi-bin/statspost.ha":
            return html("", 302, {"location": "/cgi-bin/lanstatistics.ha"})
        return html(after_page() if state["posted"] else EMPTY)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*SCAN, *(["--json"] if json_out else [])])
    out = capsys.readouterr().out
    return code, (json.loads(out) if json_out and out.strip() else out), wire


def test_parse_congestion_reads_time_and_channels():
    parsed = parse_congestion(FORM_HEAD + _results(datetime(2026, 11, 24, 15, 1, 36)) + FORM_TAIL)
    assert parsed is not None
    assert parsed["time"] == "Tue Nov 24 15:01:36 2026"
    assert parsed["channels"] == [
        {"Channel": "36", "AP Count": "13", "Congestion Score": "8"},
        {"Channel": "40", "AP Count": "2", "Congestion Score": "10"},
    ]


def test_parse_congestion_without_a_table_is_none():
    assert parse_congestion(EMPTY) is None


def test_a_run_reports_its_congestion_table(monkeypatch, capsys, tmp_env, clock):
    code, out, wire = _run(monkeypatch, capsys, lambda: FORM_HEAD + _results(datetime.now()) + FORM_TAIL)
    assert code == 0, out
    assert out["committed"] is True
    assert out["answer"]["title"] == "Wi-Fi Congestion"
    assert out["answer"]["tables"][0] == {"Channel": "36", "AP Count": "13", "Congestion Score": "8"}
    assert "Time" in out["answer"]["values"]
    gets = [urlsplit(r.url).path for r in wire.requests if r.method == "GET"]
    assert "/cgi-bin/statspost.ha" in gets, "the hand-off that runs the detection is requested"


def test_a_run_prints_its_congestion_table(monkeypatch, capsys, tmp_env, clock):
    code, out, _ = _run(
        monkeypatch, capsys, lambda: FORM_HEAD + _results(datetime.now()) + FORM_TAIL, json_out=False
    )
    assert code == 0
    assert "Wi-Fi Congestion" in out and "Congestion Score" in out and "13" in out


@pytest.mark.parametrize(
    "after",
    [lambda: EMPTY, lambda: FORM_HEAD + _results(datetime.now() - timedelta(days=1)) + FORM_TAIL],
    ids=["no-table", "stale-table"],
)
def test_a_run_without_fresh_results_is_unconfirmed(monkeypatch, capsys, tmp_env, clock, after):
    code, out, _ = _run(monkeypatch, capsys, after)
    assert code == 2, out
    assert out["committed"] is False
    assert "congestion results" in out["warning"]


def test_a_refused_run_stays_a_rejection(monkeypatch, capsys, tmp_env, clock):
    code, out, _ = _run(monkeypatch, capsys, lambda: REFUSED)
    assert code == 1, out
    assert out["committed"] is False and "DFS" in out["warning"]
    assert "answer" not in out
