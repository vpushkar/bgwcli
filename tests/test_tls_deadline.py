"""The TLS handshake and a slow TCP connect share the request's one total deadline."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from bgwcli.client import HttpRequest, ResponseReadTimeout, urllib_transport


def _stalling_tls_server():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    held: list[socket.socket] = []

    def run():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # accepted, never answered: the handshake stalls

    threading.Thread(target=run, daemon=True).start()
    return server, held


def test_a_stalled_handshake_after_a_slow_connect_ends_at_the_total_deadline(monkeypatch):
    server, held = _stalling_tls_server()
    real_connect = socket.socket.connect

    def slow_connect(self, address):
        time.sleep(0.25)  # TCP establishment uses part of the budget, then succeeds
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", slow_connect)
    request = HttpRequest("GET", f"https://127.0.0.1:{server.getsockname()[1]}/", {}, None, 500, True)
    started = time.monotonic()
    try:
        with pytest.raises(Exception) as caught:
            urllib_transport(request)
        elapsed = time.monotonic() - started
    finally:
        server.close()
        for conn in held:
            conn.close()
    assert elapsed < 0.62, f"handshake overran the 500 ms deadline: {elapsed:.2f}s"
    # No request had been sent during the handshake: never a response-read timeout.
    assert not isinstance(caught.value, ResponseReadTimeout)


def test_a_completed_handshake_serves_the_response(tmp_path):
    import http.server
    import shutil
    import ssl
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to mint a throwaway certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"hello over tls"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = urllib_transport(
            HttpRequest("GET", f"https://127.0.0.1:{server.server_address[1]}/", {}, None, 5000, True)
        )
    finally:
        server.shutdown()
        server.server_close()
    assert response.status == 200 and response.body == b"hello over tls"
