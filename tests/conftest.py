import pytest


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    """Isolate every env var the CLI reads and point caches/dumps at tmp_path."""
    for name in [
        "BGW_HOST", "ROUTER_IP", "BGW_ACCESS_CODE", "BGW_TIMEOUT_MS", "BGW_INSECURE_TLS", "BGW_WAIT_FOR_SESSION",
        "BGW_SESSION_WAIT_TIMEOUT_MS", "BGW_SESSION_WAIT_INTERVAL_MS", "BGW_SESSION_CACHE_TTL_MS",
        "BGW_SESSION_POOL_COOLDOWN_MS", "BGW_SESSION_LOCK_TIMEOUT_MS", "BGW_DUMP_DIR", "XDG_STATE_HOME",
        "BGW_SESSION_CACHE_DIR", "XDG_CACHE_HOME", "BGW_SESSION_LOCK_STALE_MS",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BGW_DUMP_DIR", str(tmp_path / "dumps"))
    return tmp_path
