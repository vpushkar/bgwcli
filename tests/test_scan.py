from test_sweep import FakeClient, backend  # noqa: F401 - fixture import

from bgwcli.scan import scan_router
from bgwcli.sweep import SweepOptions, sweep_router
from bgwcli.types import to_json_dict


def test_scan_compatibility_path_is_the_sweep_core_metadata_path(backend):  # noqa: F811
    scan = scan_router(FakeClient(), delay_ms=0, pages=["diag"])
    swept = sweep_router(FakeClient(), SweepOptions(delay_ms=0, pages=["diag"]))
    assert scan == swept
    assert to_json_dict(scan) == to_json_dict(swept)


def test_scan_uses_fallbacks_and_forwards_detail_flags(backend):  # noqa: F811
    fallback = scan_router(FakeClient(fail_pages={"home"}), delay_ms=0, pages=["home"])
    assert fallback[0].fallback is True and fallback[0].ok is True

    detailed = scan_router(FakeClient(), delay_ms=0, pages=["diag"], include_parsed=True, include_forms=True)
    assert detailed[0].parsed is not None and detailed[0].controls is not None
    assert detailed[0].raw_html is None  # scan never captures raw HTML

    compact = scan_router(FakeClient(), delay_ms=0, pages=["diag"])
    assert compact[0].parsed is None and compact[0].controls is None


def test_scan_honours_delay(backend):  # noqa: F811
    scan_router(FakeClient(), delay_ms=100, pages=["diag", "dhcpserver"])
    assert backend == [100, 100]
