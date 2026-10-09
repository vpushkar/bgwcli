"""HTTP client for the BGW320 web UI (port of BGW320-CLI src/client.ts).

Transport is urllib.request with redirects never followed automatically: GET requests are
followed manually (max 5 hops) exactly like the TypeScript client, POSTs are not, so callers
such as restore can observe the router's ``302 Location: /cgi-bin/home.ha`` answer.

Cookies live in a plain dict in insertion order so the shared session JSON
(``RouterSessionSnapshot``) round-trips byte-for-byte with the TypeScript CLI.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode, urljoin, urlsplit

from . import __version__
from .config import GlobalOptions
from .errors import BgwError, RouterAuthError, RouterConnectionError, RouterSessionPoolFullError, UsageError
from .pages import canonical_cgi_page, form_target
from .types import HttpMethod, HttpResponse, RouterSessionSnapshot

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_USER_AGENT = f"bgw/{__version__}"
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_DEFAULT_PORTS = {"http": 80, "https": 443}

_sleep = time.sleep


class RouterResponseError(BgwError):
    """The router answered with an HTTP error status or an oversized body (exit 2: no answer)."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class NoncePageRefusedError(RouterResponseError):
    """The page a write's nonce is read from is not a usable form page (Login, Page-not-found,
    "Please wait", parser-cut, no form control, no nonce for the target form): refused before any
    POST. A structural answer from the gateway (still exit 2 on every command), not a transport
    fault, so autorestore does not treat it as the router being unreachable."""


def session_pool_full_error(
    message: str = "Router web session pool is full.", *, waited_ms: int = 0, retry_count: int = 0
) -> RouterSessionPoolFullError:
    """Build the shared RouterSessionPoolFullError carrying the TS ``waitedMs`` / ``retryCount`` metadata."""
    error = RouterSessionPoolFullError(message)
    error.session_pool_full = True  # type: ignore[attr-defined]
    error.waited_ms = waited_ms  # type: ignore[attr-defined]
    error.retry_count = retry_count  # type: ignore[attr-defined]
    return error


def pool_full_metadata(value: Any) -> tuple[int, int]:
    """Normalize one observed wait/retry pair from an error or structured result."""
    get = value.get if isinstance(value, Mapping) else lambda name, default=None: getattr(value, name, default)
    return int(get("waitedMs", get("waited_ms", 0)) or 0), int(get("retryCount", get("retry_count", 0)) or 0)


@dataclass(frozen=True)
class WriteObservation:
    """Cumulative non-login POST transport attempts and returned transport responses.

    An attempt alone does not prove delivery or a gateway configuration change.
    """

    attempts: int
    responses: int
    # HTTP status of the most recent configuration POST that returned a response (None before any).
    last_status: int | None = None


@runtime_checkable
class WriteObserver(Protocol):
    def observe_writes(self) -> WriteObservation: ...


@dataclass
class PostWriteEvidence:
    """What the transport saw of one write: whether any configuration POST went out, whether the
    LAST one sent was answered (a Login-page answer re-sends once, so there can be two), how many
    were sent and the HTTP status of the last answered one."""

    attempted: bool | None = None
    response_received: bool | None = None
    attempts: int | None = None
    status_code: int | None = None


def _write_observation(client: Any) -> WriteObservation | None:
    """An optional diagnostic must never replace the operation's own result."""
    try:
        value = client.observe_writes() if isinstance(client, WriteObserver) else None
        if (isinstance(value, WriteObservation)
                and type(value.attempts) is int and type(value.responses) is int
                and 0 <= value.responses <= value.attempts):
            return value
    except Exception:  # noqa: BLE001 - broken observers degrade to unknown delivery
        pass
    return None


@contextmanager
def observe_post(client: Any, evidence: PostWriteEvidence | None = None) -> Iterator[PostWriteEvidence]:
    """Observe one POST without allowing optional instrumentation to mask its result.

    Callers set both fields True for a returned response. Invalid/reset counters
    retain unknown delivery; KeyboardInterrupt and SystemExit remain cancellation.
    """
    evidence = evidence if evidence is not None else PostWriteEvidence()
    before = _write_observation(client)
    cancelled = False
    try:
        yield evidence
    except BaseException as exc:
        cancelled = not isinstance(exc, Exception)
        raise
    finally:
        if before is not None and not cancelled:
            after = _write_observation(client)
            if after is not None:
                attempts = after.attempts - before.attempts
                responses = after.responses - before.responses
                if 0 <= responses <= attempts:
                    evidence.attempted = evidence.attempted or attempts > 0
                    if attempts > 0:
                        # POSTs are sequential: the last one was answered only when every one was.
                        evidence.response_received = responses == attempts
                        evidence.attempts = (evidence.attempts or 0) + attempts
                        last = getattr(after, "last_status", None)
                        evidence.status_code = last if responses == attempts and type(last) is int else None
                    else:
                        evidence.response_received = evidence.response_received or False


# --- transport ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequest:
    method: HttpMethod
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_ms: int
    insecure_tls: bool
    max_bytes: int = MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class RawResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


Transport = Callable[[HttpRequest], RawResponse]


class ResponseReadTimeout(TimeoutError):
    """The connection was made and the request sent, but the response did not arrive in time.

    Transports raise it to tell a read-phase timeout apart from a connect-phase one: the gateway may
    already have acted on the request. A plain TimeoutError keeps the connect wording.
    """


class ResponseEndedEarly(ConnectionError):
    """The connection closed before the body reached its declared Content-Length.

    The request went out and a (partial) answer came back, so the gateway may have acted on it; the
    truncated page itself is never returned as if it were complete.
    """


class _PassThroughErrors(urllib.request.HTTPErrorProcessor):
    """Return every status as a response: no HTTPError, and therefore no automatic redirects."""

    def http_response(self, request, response):  # noqa: D401 - urllib hook
        return response

    https_response = http_response


class _DeadlineWatchdog:
    """Shut the connection's socket down when the request's total deadline passes.

    Per-socket-operation timeouts cannot bound a gateway that dribbles its status line or headers one
    byte at a time; shutting the socket down from a timer makes the blocked read fail at once, in
    every phase (status line, headers, body). ``socket.socket.shutdown`` is called on the base class
    so a TLS socket is cut at the TCP level without touching its SSL object from this thread.
    """

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.fired = False
        self.attached = False
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def hold(self, sock: socket.socket) -> None:
        """Watch `sock` without claiming the request went out (a TLS handshake is still running)."""
        with self._lock:
            self._sock = sock
            if self.fired:
                self._shutdown()

    def attach(self, sock: socket.socket) -> None:
        """Watch `sock` once the connection is ready for the request: from here a deadline cut means
        the request may already have reached the gateway."""
        with self._lock:
            self._sock = sock
            self.attached = True
            if self.fired:
                self._shutdown()

    def cancel(self) -> None:
        self._timer.cancel()

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            self._shutdown()

    def _shutdown(self) -> None:
        if self._sock is None:
            return
        with suppress(OSError):
            socket.socket.shutdown(self._sock, socket.SHUT_RDWR)


def _resolve_within(host: str, port: int, seconds: float) -> list[tuple[Any, ...]]:
    """getaddrinfo bounded by the time left: the lookup runs on a daemon thread and is waited on only
    until the deadline. A lookup still running then is abandoned; its late answer is never used."""
    outcome: dict[str, Any] = {}

    def lookup() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread
            outcome["error"] = exc

    worker = threading.Thread(target=lookup, name="bgw-resolve", daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        raise TimeoutError(f"resolving {host} exceeded the request deadline")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["infos"]


def _deadline_connection(watchdog: _DeadlineWatchdog) -> Callable[..., socket.socket]:
    """socket.create_connection with name resolution and every connect attempt inside the deadline."""

    def create_connection(address: tuple[str, int], timeout: Any = None, source_address: Any = None, *_a, **_k):
        host, port = address
        infos = _resolve_within(host, port, watchdog.remaining())
        last_error: BaseException | None = None
        for family, kind, proto, _canonical, sockaddr in infos:
            left = watchdog.remaining()
            if left <= 0:
                raise TimeoutError(f"connecting to {host} exceeded the request deadline")
            sock = socket.socket(family, kind, proto)
            try:
                limit = left if not isinstance(timeout, (int, float)) else min(timeout, left)
                sock.settimeout(max(0.001, limit))
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                sock.close()
                last_error = exc
        raise last_error if last_error is not None else OSError(f"no address found for {host}")

    return create_connection


def _watched_connection(base: type[http.client.HTTPConnection], watchdog: _DeadlineWatchdog) -> type:
    class _WatchedConnection(base):  # type: ignore[valid-type, misc]
        def connect(self) -> None:
            # The watchdog can only cut a socket it holds: until connect() returns, the lookup and the
            # connect attempts are bounded by the time left instead.
            self._create_connection = _deadline_connection(watchdog)
            if issubclass(base, http.client.HTTPSConnection):
                self._connect_tls_within_deadline()
            else:
                super().connect()
            watchdog.attach(self.sock)

        def _connect_tls_within_deadline(self) -> None:
            """TCP connect, then the TLS handshake on the time that is left: the handshake gets its
            own socket timeout and is watched by the deadline timer from its first byte (it must not
            inherit the pre-connect timeout, nor wait for the connection to be handed over)."""
            http.client.HTTPConnection.connect(self)
            tcp = self.sock
            left = watchdog.remaining()
            if left <= 0:
                tcp.close()
                raise TimeoutError(f"connecting to {self.host} exceeded the request deadline")
            secured = self._context.wrap_socket(tcp, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock = secured
            watchdog.hold(secured)
            try:
                secured.settimeout(max(0.001, left))
                secured.do_handshake()
            except BaseException:
                secured.close()
                raise

    return _WatchedConnection


class _WatchedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, watchdog: _DeadlineWatchdog) -> None:
        super().__init__()
        self._watchdog = watchdog

    def http_open(self, req):  # noqa: D401 - urllib hook
        return self.do_open(_watched_connection(http.client.HTTPConnection, self._watchdog), req)


class _WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, watchdog: _DeadlineWatchdog, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._watchdog = watchdog
        self._tls_context = context

    def https_open(self, req):  # noqa: D401 - urllib hook
        connection = _watched_connection(http.client.HTTPSConnection, self._watchdog)
        return self.do_open(connection, req, context=self._tls_context)


def urllib_transport(request: HttpRequest) -> RawResponse:
    # Total deadline, like the TS AbortController: urllib's timeout only bounds each socket
    # operation, so a router dribbling one byte at a time could otherwise hold the CLI forever.
    seconds = max(0.001, request.timeout_ms / 1000)
    deadline = time.monotonic() + seconds
    remaining = lambda: deadline - time.monotonic()  # noqa: E731
    watchdog = _DeadlineWatchdog(seconds)
    try:
        return _urllib_exchange(request, watchdog, remaining)
    except Exception as exc:
        # Once the socket is connected the request has (at least partly) gone out, so a deadline cut
        # in any later phase is a response-read timeout: the gateway may already have acted on it.
        if watchdog.fired and watchdog.attached and not isinstance(exc, ResponseReadTimeout):
            raise ResponseReadTimeout(f"response exceeded the {request.timeout_ms} ms deadline") from exc
        raise
    finally:
        watchdog.cancel()


def _urllib_exchange(
    request: HttpRequest, watchdog: _DeadlineWatchdog, remaining: Callable[[], float]
) -> RawResponse:
    # ProxyHandler({}) replaces the default environment/system proxy handler: the gateway is a LAN
    # device and its session cookies and access-code hash must never be sent through a proxy.
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({}),
        _PassThroughErrors(),
        _WatchedHTTPHandler(watchdog),
    ]
    if urlsplit(request.url).scheme == "https":
        if request.insecure_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        handlers.append(_WatchedHTTPSHandler(watchdog, context))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(request.url, data=request.body, method=request.method)
    for name, value in request.headers.items():
        req.add_header(name, value)
    try:
        # urllib wraps connect/send failures (including their timeouts) in URLError; a bare timeout
        # out of open() comes from waiting for the status line, after the request went out.
        opened = opener.open(req, timeout=max(0.001, remaining()))
    except urllib.error.URLError:
        raise
    except TimeoutError as exc:
        raise ResponseReadTimeout(str(exc) or "timed out waiting for response headers") from exc
    with opened as response:
        chunks: list[bytes] = []
        total = 0
        try:
            while total <= request.max_bytes:
                left = remaining()
                if left <= 0:
                    raise TimeoutError(f"response body exceeded the {request.timeout_ms} ms deadline")
                sock = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(sock, "_sock", None)
                if sock is not None:
                    sock.settimeout(left)
                want = min(65536, request.max_bytes + 1 - total)
                chunk = response.read1(want) if hasattr(response, "read1") else response.read(want)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except TimeoutError as exc:
            raise ResponseReadTimeout(str(exc) or "timed out reading the response body") from exc
        missing = response.length
        if total <= request.max_bytes and isinstance(missing, int) and missing > 0:
            # http.client's read1 returns a short read (not IncompleteRead) when the peer closes
            # before Content-Length bytes arrived; the remaining count is still on response.length.
            if watchdog.fired:
                raise ResponseReadTimeout(f"response body exceeded the {request.timeout_ms} ms deadline")
            raise ResponseEndedEarly(f"received {total} of {total + missing} declared bytes")
        if watchdog.fired and response.length is None and not getattr(response, "chunked", False):
            # Without a declared length the shutdown surfaces as a clean EOF: never return a
            # possibly truncated page. (A declared length or chunking reports truncation itself.)
            raise ResponseReadTimeout(f"response body exceeded the {request.timeout_ms} ms deadline")
        body = b"".join(chunks)
        return RawResponse(
            status=response.status,
            reason=response.reason or "",
            headers=list(response.headers.items()),
            body=body,
        )


# --- protocols other modules can accept ----------------------------------------------------------


class PageGetter(Protocol):
    def get_cgi_page(self, page: str, *, auth: bool = True) -> HttpResponse: ...


# --- client ---------------------------------------------------------------------------------------


@dataclass
class RouterClientOptions:
    host: str
    access_code: str | None = None
    timeout_ms: int = 15000
    # True when the user set --timeout / BGW_TIMEOUT_MS; then SLOW_PAGE_TIMEOUT_MS floors do not apply.
    timeout_explicit: bool = False
    insecure_tls: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    wait_for_session: bool = False
    session_wait_timeout_ms: int = 120000
    session_wait_interval_ms: int = 10000
    on_session_wait: Callable[[dict[str, int]], None] | None = field(default=None, repr=False)


# Pages that legitimately take longer than the 15 s default on this gateway (measured live
# 2026-09-20: home.ha 17-18 s, lanstatistics.ha 23-29 s). Used as a floor for the implicit default
# only; an explicit --timeout / BGW_TIMEOUT_MS is always honored as-is.
# Protected page read to tell a live session from a dead one: the login handshake verifies a login
# that did not answer with the usual 302 -> home.ha with it, and `check` probes the held session.
SESSION_PROBE_PAGE = "services"
SLOW_PAGE_TIMEOUT_MS: dict[str, int] = {"home": 45000, "lanstatistics": 45000}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_CGI_PAGE_IN_PATH = re.compile(r"^/cgi-bin/([a-z0-9_]+)\.ha", re.IGNORECASE)


def effective_timeout_ms(path: str, timeout_ms: int, timeout_explicit: bool) -> int:
    if timeout_explicit:
        return timeout_ms
    match = _CGI_PAGE_IN_PATH.match(path)
    floor = SLOW_PAGE_TIMEOUT_MS.get(match.group(1).lower(), 0) if match else 0
    return max(timeout_ms, floor)


class BGW320Client:
    def __init__(
        self,
        host: str,
        *,
        access_code: str | None = None,
        timeout_ms: int = 15000,
        timeout_explicit: bool = False,
        insecure_tls: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        wait_for_session: bool = False,
        session_wait_timeout_ms: int = 120000,
        session_wait_interval_ms: int = 10000,
        on_session_wait: Callable[[dict[str, int]], None] | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.options = RouterClientOptions(
            host=host,
            access_code=access_code,
            timeout_ms=timeout_ms,
            timeout_explicit=timeout_explicit,
            insecure_tls=insecure_tls,
            user_agent=user_agent,
            wait_for_session=wait_for_session,
            session_wait_timeout_ms=session_wait_timeout_ms,
            session_wait_interval_ms=session_wait_interval_ms,
            on_session_wait=on_session_wait,
        )
        self._origin = normalize_origin(host)
        self._transport: Transport = transport or urllib_transport
        self._cookies: dict[str, str] = {}
        self._authenticated = False
        # Logins this client started (an already-authenticated no-op login() is not counted). Callers
        # compare it around a request to learn whether the client logged in again on its own.
        self.login_attempts = 0
        # Authenticated page answers (GET auth=True, form POSTs) that came back without a login
        # bounce or an error: each one shows the router still honours the session's cookies.
        self.authenticated_requests = 0
        # Count configuration POST transport attempts, excluding authentication POSTs.
        self._cgi_post_attempts = 0
        self._cgi_post_responses = 0
        self._cgi_post_last_status: int | None = None

    def observe_writes(self) -> WriteObservation:
        """Snapshot POST transport evidence, including responses rejected by HTTP validation."""
        return WriteObservation(self._cgi_post_attempts, self._cgi_post_responses, self._cgi_post_last_status)

    @classmethod
    def from_options(
        cls,
        options: GlobalOptions,
        access_code: str | None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        on_session_wait: Callable[[dict[str, int]], None] | None = None,
        transport: Transport | None = None,
    ) -> BGW320Client:
        return cls(
            options.host,
            access_code=access_code,
            timeout_ms=options.timeout_ms,
            timeout_explicit=getattr(options, "timeout_explicit", False),
            insecure_tls=options.insecure_tls,
            user_agent=user_agent,
            wait_for_session=options.wait_for_session,
            session_wait_timeout_ms=options.session_wait_timeout_ms,
            session_wait_interval_ms=options.session_wait_interval_ms,
            on_session_wait=on_session_wait,
            transport=transport,
        )

    # -- session -------------------------------------------------------------------------------

    def session_identity(self) -> str:
        return self._origin

    def has_authenticated_session(self) -> bool:
        return self._authenticated and len(self._cookies) > 0

    def export_session(self) -> RouterSessionSnapshot:
        return RouterSessionSnapshot(
            origin=self._origin, authenticated=self._authenticated, cookies=dict(self._cookies)
        )

    def import_session(self, snapshot: RouterSessionSnapshot | Mapping[str, Any]) -> None:
        if isinstance(snapshot, Mapping):
            origin, authenticated, cookies = (
                snapshot.get("origin"),
                snapshot.get("authenticated"),
                snapshot.get("cookies"),
            )
        else:
            origin, authenticated, cookies = snapshot.origin, snapshot.authenticated, snapshot.cookies
        if origin != self._origin or authenticated is not True:
            return
        self._cookies.clear()
        if isinstance(cookies, Mapping):
            for name, value in cookies.items():
                # Only text name/value pairs without control characters can be sent back in a Cookie header.
                if isinstance(name, str) and isinstance(value, str) and name and value and not (
                    _CONTROL_CHARACTERS.search(name) or _CONTROL_CHARACTERS.search(value)
                ):
                    self._cookies[name] = value
        self._authenticated = len(self._cookies) > 0

    def clear_session(self) -> None:
        self._cookies.clear()
        self._authenticated = False

    def probe_session_accepted(self) -> bool:
        """True when the gateway serves the protected probe page to the held session cookies.

        Never logs in. A Login-page answer (including a redirect to login.ha) or a 401/403 means the
        session is not accepted: the session is cleared, so a dead cached session is no longer
        flagged authenticated and a Set-Cookie picked up from login.ha is never kept as a live
        session (the session coordinator then forgets the cached copy). A transport fault or another
        HTTP error is no answer about the session: it reads as not accepted, and the session held
        before the probe is restored untouched. A full session pool is no answer either: with --wait-for-session the
        wait loop polls until a slot frees and the session is probed once more; otherwise, or when
        the wait budget runs out, the pool-full error propagates.
        """
        from .parser import looks_like_login

        cookies_before, authenticated_before = dict(self._cookies), self._authenticated

        def probe() -> HttpResponse | None:
            try:
                return self._get_public_page(SESSION_PROBE_PAGE)
            except RouterAuthError:
                raise
            except BgwError:
                self._cookies = dict(cookies_before)
                self._authenticated = authenticated_before
                return None

        response = probe()
        # auth=False returns the raw answer, so the pool-full page (a Login page) is recognised here.
        if response is not None and router_sessions_full(response.body):
            self.wait_for_free_session()
            response = probe()
            if response is not None and router_sessions_full(response.body):
                raise session_pool_full_error()
        if response is None:
            return False
        if looks_like_login(response.body) or _is_login_url(response.url) or response.status_code in (401, 403):
            self.clear_session()
            return False
        if 200 <= response.status_code < 300:
            return True
        # Any other HTTP error (5xx, 404) is the page failing, not a verdict on the session.
        self._cookies = dict(cookies_before)
        self._authenticated = authenticated_before
        return False

    # -- pages -----------------------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        response = self.get_cgi_page("sitemap", auth=False)  # a full session pool raises, never "reachable"
        return {
            "host": urlsplit(self._origin).netloc,
            "reachable": 200 <= response.status_code < 500,
            "title": title_of(response.body),
            "authenticated": self._authenticated,
        }

    def _get_public_page(self, page: str) -> HttpResponse:
        """The raw answer to an unauthenticated read (nothing is checked, nothing is logged in)."""
        return self._request(f"/cgi-bin/{canonical_cgi_page(page)}.ha", "GET")

    def get_cgi_page(self, page: str, *, auth: bool = True) -> HttpResponse:
        """GET a CGI page. ``auth=False`` is a public read: it returns the raw answer, but the pool-full
        page is never data (a full session pool raises, so callers record the cooldown)."""
        from .parser import looks_like_login

        page = canonical_cgi_page(page)
        path = f"/cgi-bin/{page}.ha"
        response = self._request(path, "GET")
        if not auth:
            if router_sessions_full(response.body):
                raise session_pool_full_error()
            return response
        pool_full = router_sessions_full(response.body)
        # _request follows redirects, so this answer may come from login.ha rather than the page.
        bounced = looks_like_login(response.body) or _is_login_url(response.url)
        if pool_full and not self._can_wait_and_relogin():
            # A full pool is the answer, whatever credentials are held: classify it before any
            # credential-dependent forced login so the cooldown is recorded and the error is not
            # replaced by "Access code required".
            self.clear_session()
            raise session_pool_full_error()
        if bounced and not pool_full and not 200 <= response.status_code < 300:
            # login.ha (or its page) answering with an HTTP error is still the gateway refusing the
            # session: forget it before the error is raised (pool-full keeps precedence below).
            self.clear_session()
        if not (pool_full and self.options.wait_for_session):
            # A 401/403 is page-level only when the content page itself answers it. One answered by
            # login.ha, or carrying the Login page body, means the session is gone: not page-level,
            # so _require_successful_response clears the authenticated flag and callers abort.
            self._require_successful_response(response, page_level=not (bounced or pool_full))
        if bounced or pool_full:
            self.login(response.body, force=True)
            retry = self._request(path, "GET")
            # Same rule after the fresh login: a Login page, or any answer whose final URL is login.ha
            # (whatever its body or status), is a lost session, not the page.
            retry_bounced = looks_like_login(retry.body) or _is_login_url(retry.url)
            if retry_bounced:
                # A lost session whatever the status: a pool-full body keeps precedence, an HTTP error
                # status never keeps (or caches) the session the gateway just refused.
                self.clear_session()
                self._require_successful_response(retry, page_level=False)
                raise RouterAuthError(
                    "Router returned the login page after authentication. "
                    "Wait for stale router web sessions to expire, then retry."
                )
            self._require_successful_response(retry, page_level=True)
            self.authenticated_requests += 1
            return retry
        self.authenticated_requests += 1
        return response

    def _can_wait_and_relogin(self) -> bool:
        """A pool-full answer is waited out and retried only with --wait-for-session AND an access code
        to log in with; otherwise it is the final answer."""
        return self.options.wait_for_session is True and bool(self.options.access_code)

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse:
        """POST a page's own form: nonce from GET <page>.ha, POST to /cgi-bin/<page>.ha."""
        page = canonical_cgi_page(page)
        return self.post_form(page, f"{page}.ha", fields)

    def post_form(self, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> HttpResponse:
        """POST to an arbitrary CGI path (e.g. ``wrestart.ha?1``) with the nonces read from
        ``nonce_page``. home.ha hosts several Restart forms whose actions are other CGI scripts
        with a query string; the nonces they carry are the ones served on home.ha.

        Public nonce pages (home.ha) never trigger the GET auto-login, so authenticate first when
        no session exists; and because a cached session can be stale, a Login-page answer to the
        POST triggers one forced re-login + retry (observed live 2026-09-20).
        """
        from .parser import looks_like_login

        nonce_page = canonical_cgi_page(nonce_page)
        if not self.has_authenticated_session():
            self.login()
        response = self._post_form_once(nonce_page, post_path, fields)
        if router_sessions_full(response.body) and not self._can_wait_and_relogin():
            # A full pool is the answer, whatever credentials are held: classify it before any
            # credential-dependent forced login so the cooldown is recorded and the error is not
            # replaced by "Access code required".
            self.clear_session()
            raise session_pool_full_error()
        if looks_like_login(response.body):
            self.login(response.body, force=True)
            try:
                response = self._post_form_once(nonce_page, post_path, fields)
            except NoncePageRefusedError as refusal:
                # The first POST went out and was answered with the Login page, so the refusal's own
                # "nothing was sent" clause is untrue here: it only covers the re-send.
                reason = str(refusal).replace("; nothing was sent", "")
                raise NoncePageRefusedError(
                    f"{reason}; the first POST was answered with the Login page and the re-send was refused"
                ) from refusal
        if router_sessions_full(response.body):
            self.clear_session()
            raise session_pool_full_error()
        if looks_like_login(response.body):
            self.clear_session()
            raise RouterAuthError("Router returned the login page instead of accepting the operation.")
        if _redirects_to_login(response):
            # The gateway bounced the write to its login handshake: the session is gone and the
            # change was not accepted. Not re-sent: only a Login-page body triggers the one retry.
            self.clear_session()
            raise RouterAuthError("Router redirected the operation to the login page instead of accepting it.")
        if response.status_code >= 400:
            self._require_successful_response(response, page_level=True)
        self.authenticated_requests += 1
        return response

    def _post_form_once(self, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> HttpResponse:
        from .fetch import is_page_not_found
        from .parser import looks_like_login, parse_page

        current = self.get_cgi_page(nonce_page)
        path = f"/cgi-bin/{post_path}"
        # The page this POST's nonce is read from must be a real, whole form page: a Login, Page-not-
        # found, "Please wait" or parser-cut answer would yield a POST the gateway cannot accept (or
        # one built from a partial page). Nothing has been sent yet when any of these raise.
        parsed = parse_page(nonce_page, current.body)
        if looks_like_login(current.body):
            raise NoncePageRefusedError(f"{nonce_page}: the page read for the write nonce is the login page")
        if is_page_not_found(parsed):
            raise NoncePageRefusedError(f"{nonce_page}: the page read for the write nonce is a page-not-found page")
        if parsed.truncated:
            raise NoncePageRefusedError(
                f"{nonce_page}: the page read for the write nonce is larger than the parser's bounds"
            )
        # The hidden nonce/hashpassword inputs are the client's own plumbing, not form controls.
        if not (
            any(f.name.lower() not in ("nonce", "hashpassword") for f in parsed.fields)
            or parsed.selects or parsed.textareas or parsed.buttons
        ):
            raise NoncePageRefusedError(f"{nonce_page}: the page read for the write nonce has no form controls")
        # A page can host several forms, each with its own nonce (home.ha's Restart forms carry two).
        nonces = _select_write_nonces(current.body, nonce_page, post_path, fields)
        pairs = [(k, v) for k, v in fields.items() if k != "nonce"] + [("nonce", n) for n in nonces]
        referer = f"{self._origin}/cgi-bin/{nonce_page}.ha"
        return self._request(
            path,
            "POST",
            body=urlencode(pairs),
            headers={"Referer": referer, "Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    # -- login -----------------------------------------------------------------------------------

    def login(self, initial_login_html: str | None = None, *, force: bool = False) -> None:
        from .parser import looks_like_login

        if self._authenticated and not force:
            return
        self.login_attempts += 1
        if force:
            self._authenticated = False
        access_code = self.options.access_code
        if not access_code:
            raise RouterAuthError("Access code required. Set BGW_ACCESS_CODE or pass --access-code-stdin.")

        nonce = extract_nonce(initial_login_html) if initial_login_html else None
        if not nonce and initial_login_html and router_sessions_full(initial_login_html):
            nonce = self._wait_for_session_nonce()
        attempt = 0
        while attempt < 8 and not nonce:
            if attempt > 1:
                self._cookies.clear()
            login_page = self._request("/cgi-bin/login.ha", "GET")
            if router_sessions_full(login_page.body):
                nonce = self._wait_for_session_nonce()
                break
            self._require_successful_response(login_page)
            nonce = extract_nonce(login_page.body)
            if not nonce:
                _sleep((150 + attempt * 100) / 1000)
                login_page = self._request("/cgi-bin/login.ha", "GET")
                if router_sessions_full(login_page.body):
                    nonce = self._wait_for_session_nonce()
                    break
                self._require_successful_response(login_page)
                nonce = extract_nonce(login_page.body)
            if not nonce:
                _sleep((250 + attempt * 150) / 1000)
            attempt += 1

        if not nonce:
            raise RouterAuthError("Router did not return a login nonce after retrying the cookie handshake.")

        hashpassword = hashlib.md5(f"{access_code}{nonce}".encode()).hexdigest()  # noqa: S324 - router protocol
        body = urlencode(
            {
                "nonce": nonce,
                "password": "*" * len(access_code),
                "hashpassword": hashpassword,
                "Continue": "Continue",
            }
        )
        response = self._request(
            "/cgi-bin/login.ha",
            "POST",
            body=body,
            headers={
                "Referer": self._origin + "/cgi-bin/login.ha",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )

        self._require_successful_response(response, allow_redirect=True)
        location = response.headers.get("location", "")
        if isinstance(location, list):
            location = location[0] if location else ""
        if response.status_code == 302 and re.search(r"/cgi-bin/home\.ha", location, re.I):
            self._authenticated = True
            return
        if looks_like_login(response.body) or re.search(r"Login Failed|Access Code Required", response.body, re.I):
            raise RouterAuthError("Login failed. Check the device access code.")
        # No redirect to home.ha and no explicit failure: the gateway can answer a wrong code with an
        # ordinary 200 page while leaving the session unauthenticated (observed live 2026-09-20).
        # Verify against a protected page so a rejected code fails HERE, where callers (e.g. the
        # autorestore fallback code) can react, and so no unauthenticated session is ever cached.
        probe = self._request(f"/cgi-bin/{SESSION_PROBE_PAGE}.ha", "GET")
        self._require_successful_response(probe)
        # A probe redirected to login.ha was refused even when that page carries no Login markup.
        if looks_like_login(probe.body) or _is_login_url(probe.url):
            self.clear_session()
            raise RouterAuthError("Login failed. Check the device access code.")
        self._authenticated = True

    def wait_for_free_session(self) -> None:
        """Poll the login page until the gateway no longer reports a full session pool, within the
        --wait-for-session budget. Raises the pool-full error at once when waiting is off, or with
        the observed wait when the budget runs out. Never logs in."""
        self._wait_for_session_nonce()

    def _wait_for_session_nonce(self) -> str | None:
        if self.options.wait_for_session is not True:
            raise session_pool_full_error()
        timeout_ms = max(0, self.options.session_wait_timeout_ms)
        interval_ms = max(1, self.options.session_wait_interval_ms)
        start = _now_ms()
        retry_count = 0
        waited_ms = 0
        while waited_ms < timeout_ms:
            if self.options.on_session_wait:
                self.options.on_session_wait(
                    {
                        "waitedMs": waited_ms,
                        "retryCount": retry_count,
                        "timeoutMs": timeout_ms,
                        "intervalMs": interval_ms,
                    }
                )
            _sleep(min(interval_ms, max(0, timeout_ms - waited_ms)) / 1000)
            retry_count += 1
            waited_ms = _now_ms() - start
            login_page = self._request("/cgi-bin/login.ha", "GET")
            if router_sessions_full(login_page.body):
                continue
            self._require_successful_response(login_page)
            return extract_nonce(login_page.body)
        # Report the wait actually observed (sleeps plus retry round trips), not the configured budget.
        raise session_pool_full_error(waited_ms=_now_ms() - start, retry_count=retry_count)

    # -- transport -------------------------------------------------------------------------------

    def _require_successful_response(
        self, response: HttpResponse, *, allow_redirect: bool = False, page_level: bool = False
    ) -> None:
        """``page_level`` marks a 401/403 answered by one content page (GET or its form POST): that
        page is forbidden, the session is not known to be lost, so the error carries
        ``page_level=True`` for per-page readers and the authenticated flag is left alone. The
        login handshake and its probe never pass it: their 401/403 is a failed login."""
        if router_sessions_full(response.body):
            self.clear_session()
            raise session_pool_full_error()
        if 200 <= response.status_code < (400 if allow_redirect else 300):
            return
        message = f"Router rejected {response.url} with HTTP {response.status_code}."
        if response.status_code in (401, 403):
            if not page_level:
                self.clear_session()
            error = RouterAuthError(message)
            error.status_code = response.status_code
            error.url = response.url
            error.page_level = page_level
            raise error
        raise RouterResponseError(message, status_code=response.status_code, url=response.url)

    def _request(
        self,
        path: str,
        method: HttpMethod,
        *,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
        redirects_remaining: int = MAX_REDIRECTS,
    ) -> HttpResponse:
        url = urljoin(self._origin + "/", path)
        request_headers: dict[str, str] = {
            "User-Agent": self.options.user_agent,
            "Accept": _ACCEPT,
            "Connection": "close",
        }
        request_headers.update(headers or {})
        cookie = self._cookie_header()
        if cookie:
            request_headers["Cookie"] = cookie
        payload = body.encode() if body else None
        if payload:
            request_headers["Content-Length"] = str(len(payload))

        request = HttpRequest(
            method=method,
            url=url,
            headers=request_headers,
            body=payload,
            timeout_ms=effective_timeout_ms(path, self.options.timeout_ms, self.options.timeout_explicit),
            insecure_tls=self.options.insecure_tls,
        )
        try:
            if method == "POST" and path != "/cgi-bin/login.ha":
                # Counted before the transport runs on purpose: even a failure while connecting cannot
                # prove that no byte reached the gateway, so the attempt stays reported as sent and
                # unanswered (conservative, never "provably not sent").
                self._cgi_post_attempts += 1
            raw = self._transport(request)
            if method == "POST" and path != "/cgi-bin/login.ha":
                self._cgi_post_responses += 1
                self._cgi_post_last_status = raw.status
        except BgwError:
            raise
        except ResponseReadTimeout as exc:
            raise RouterConnectionError(f"Timed out reading response from {url}") from exc
        except ResponseEndedEarly as exc:
            raise RouterConnectionError(f"Response from {url} ended early: {exc}") from exc
        except TimeoutError as exc:
            raise RouterConnectionError(f"Timed out connecting to {url}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise RouterConnectionError(f"Timed out connecting to {url}") from exc
            raise RouterConnectionError(str(reason) if reason is not None else str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - every transport failure is a connection error, as in TS
            raise RouterConnectionError(str(exc) or exc.__class__.__name__) from exc

        set_cookies = [value for name, value in raw.headers if name.lower() == "set-cookie"]
        self._store_cookies(set_cookies)
        response_headers = _headers_to_record(raw.headers)

        declared = response_headers.get("content-length")
        if isinstance(declared, str):
            try:
                if float(declared) > MAX_RESPONSE_BYTES:
                    raise RouterResponseError(f"Router response from {url} exceeded {MAX_RESPONSE_BYTES} bytes.")
            except ValueError:
                pass
        if len(raw.body) > MAX_RESPONSE_BYTES:
            raise RouterResponseError(f"Router response from {url} exceeded {MAX_RESPONSE_BYTES} bytes.")

        result = HttpResponse(
            status_code=raw.status,
            status_message=raw.reason,
            headers=response_headers,
            body=raw.body.decode("utf-8", errors="replace"),
            url=url,
        )

        location = response_headers.get("location")
        if isinstance(location, list):
            location = location[0] if location else None
        if follow_redirects and 300 <= raw.status < 400 and location:
            if redirects_remaining <= 0:
                raise RouterConnectionError(f"Too many redirects while requesting {url}")
            target = urlsplit(urljoin(url, location))
            next_path = target.path + (f"?{target.query}" if target.query else "")
            return self._request(next_path, "GET", redirects_remaining=redirects_remaining - 1)
        return result

    def _cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self._cookies.items())

    def _store_cookies(self, set_cookies: list[str]) -> None:
        for cookie in set_cookies:
            name, _, value = cookie.split(";", 1)[0].partition("=")
            name, value = name.strip(), value.strip()
            if not name or _CONTROL_CHARACTERS.search(name) or _CONTROL_CHARACTERS.search(value):
                continue  # nameless, or not representable in a Cookie header: dropped
            if value and not _cookie_expired(cookie):
                self._cookies[name] = value
            else:
                # An empty value, Max-Age <= 0 or a past Expires is how a server expires a cookie:
                # never keep sending the old one.
                self._cookies.pop(name, None)


# --- helpers ----------------------------------------------------------------------------------------


def _cookie_expired(set_cookie: str) -> bool:
    """True when the Set-Cookie header's own attributes expire the cookie: `Max-Age` of zero or less,
    else (an unparseable or absent Max-Age) an `Expires` date in the past. Max-Age wins over Expires,
    as in RFC 6265; unparseable values are ignored."""
    max_age: int | None = None
    expires: float | None = None
    for attribute in set_cookie.split(";")[1:]:
        key, _, value = attribute.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "max-age":
            with suppress(ValueError):
                max_age = int(value)
        elif key == "expires":
            with suppress(TypeError, ValueError, IndexError, OverflowError, OSError):
                expires = parsedate_to_datetime(value).timestamp()
    if max_age is not None:
        return max_age <= 0
    return expires is not None and expires <= time.time()


_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def normalize_origin(host: str) -> str:
    """``new URL(host).origin`` semantics: scheme://lowercase-host[:non-default-port]; https assumed.

    A host that is not a plain scheme/host/port (credentials, a path, a query, a fragment, an empty or
    invalid host name, a bad port or IPv6 literal) is a UsageError: it is never silently reduced to a
    different origin, and credentials in it are never dropped quietly."""
    def malformed(reason: str) -> UsageError:
        return UsageError(f"Invalid router host {host!r}: {reason}.")

    if re.search(r"[\x00-\x20\x7f]", host):
        raise malformed("whitespace and control characters are not allowed")
    clean = host if re.match(r"^https?://", host, re.I) else f"https://{host}"
    clean = re.sub(r"(?<=[^:/])/+$", "", clean)

    try:
        parts = urlsplit(clean)
        port = parts.port
    except ValueError as exc:
        raise malformed(str(exc)) from exc
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise malformed("credentials are not allowed; use BGW_ACCESS_CODE")
    if parts.path or parts.query or parts.fragment:
        raise malformed("give only scheme, host and optional port, without a path, query or fragment")
    hostname = (parts.hostname or "").lower()
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname.split("%", 1)[0])
        except ValueError as exc:
            raise malformed("not a valid IPv6 address") from exc
        hostname = f"[{hostname}]"
    elif not _HOSTNAME.match(hostname):
        raise malformed("not a valid host name or address")
    if port == 0:
        raise malformed("port 0 is not valid")
    scheme = parts.scheme.lower()
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"


# One forward scan over the tags: a tag body may not contain "<", so each "<" starts at most one
# bounded attempt and hostile input (a body of "<form>" or "<input name=" repeats) stays linear.
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^<>]*)>")
_ATTR = re.compile(r"""([^\s=/"'<>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
_HEX = re.compile(r"[a-f0-9]+", re.IGNORECASE)


def _attributes(raw: str) -> dict[str, str]:
    """The first value of each attribute in a tag body, in any order, quoted either way or bare."""
    found: dict[str, str] = {}
    for match in _ATTR.finditer(raw):
        name = match.group(1).lower()
        if name not in found:
            found[name] = next((g for g in match.groups()[1:] if g is not None), "")
    return found


def _nonce_value(raw: str) -> str | None:
    attrs = _attributes(raw)
    value = attrs.get("value", "")
    if attrs.get("name", "").lower() == "nonce" and _HEX.fullmatch(value):
        return value
    return None


def _scan_forms(html: str) -> list[tuple[str, list[str], list[str]]]:
    """(action, nonce values, input/button names, both in document order) of every form, in one
    pass. A form ends at its closing tag, at the next form opening or at the end of the document."""
    forms: list[tuple[str, list[str], list[str]]] = []
    current: tuple[str, list[str], list[str]] | None = None
    for match in _TAG.finditer(html):
        closing, tag, raw = match.group(1), match.group(2).lower(), match.group(3)
        if tag == "form":
            current = None if closing else (_attributes(raw).get("action", "").strip(), [], [])
            if current is not None:
                forms.append(current)
        elif tag in ("input", "button") and not closing and current is not None:
            name = _attributes(raw).get("name")
            if name:
                current[2].append(name)
            if tag == "input":
                nonce = _nonce_value(raw)
                if nonce is not None:
                    current[1].append(nonce)
    return forms


def form_nonces(html: str, action_path: str) -> list[str]:
    """Nonce values (in document order) of the form whose action equals ``action_path``
    (e.g. ``/cgi-bin/wrestart.ha?1``); empty when no such form exists. Only that form's own inputs
    count, whatever order their attributes come in."""
    for action, nonces, _names in _scan_forms(html):
        if action == action_path:
            return nonces
    return []


def _select_write_nonces(html: str, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> list[str]:
    """The nonces a write to ``post_path`` carries, read from ``html`` (the page it was served on).

    One fail-closed rule, applied in this order; any doubt raises NoncePageRefusedError before
    anything is sent, because another form's nonce is never a substitute:
    - exactly one form posts to the target: its own nonces; when it has none, the page-level nonce
      only if that form is the page's only form;
    - several forms post to the target: the one holding a control named like a posted field; if that
      does not single one out, their nonces must all be identical;
    - no form posts to the target: the page-level nonce only when the page has no form at all;
      otherwise the page's forms post elsewhere and the page is refused.
    """
    forms = _scan_forms(html)
    target = form_target(post_path)
    matching = [form for form in forms if form_target(form[0]) == target]
    page_nonce = extract_nonce(html)
    no_nonce = f"{nonce_page}: no write nonce found for {post_path}; nothing was sent"

    if len(matching) == 1:
        if matching[0][1]:
            return matching[0][1]
        if len(forms) == 1 and page_nonce:
            return [page_nonce]
        if len(forms) == 1:
            raise NoncePageRefusedError(no_nonce)
        raise NoncePageRefusedError(
            f"{nonce_page}: no write nonce found for {post_path}: the form posting to /cgi-bin/{post_path} "
            f"carries no write nonce and the page has {len(forms)} forms, so another form's nonce is not "
            "used; nothing was sent"
        )
    if matching:
        posted = [name for name in fields if name not in ("nonce", "hashpassword")]
        owners = [form for form in matching if any(name in form[2] for name in posted)]
        candidates = owners if len(owners) == 1 else (owners or matching)
        if len(owners) == 1 or all(form[1] == candidates[0][1] for form in candidates):
            if candidates[0][1]:
                return candidates[0][1]
            raise NoncePageRefusedError(no_nonce)
        raise NoncePageRefusedError(
            f"{nonce_page}: {len(matching)} forms post to /cgi-bin/{post_path} with different nonces; cannot "
            f"tell which one {', '.join(posted) or 'the posted fields'} belongs to; nothing was sent"
        )
    if forms:
        actions = ", ".join(repr(form[0] or "(no action)") for form in forms)
        raise NoncePageRefusedError(
            f"{nonce_page}: the page's form(s) post to {actions}, not to /cgi-bin/{post_path}; another form's "
            "nonce is never sent; nothing was sent"
        )
    if page_nonce:
        return [page_nonce]
    raise NoncePageRefusedError(no_nonce)


def extract_nonce(html: str) -> str | None:
    """The first nonce input on the page, attributes in either order."""
    for match in _TAG.finditer(html):
        if not match.group(1) and match.group(2).lower() == "input":
            nonce = _nonce_value(match.group(3))
            if nonce is not None:
                return nonce
    return None


def _is_login_url(url: str) -> bool:
    """True when a (redirect-followed) response's final URL is the gateway login handshake."""
    return re.search(r"/cgi-bin/login\.ha(?:[?#]|$)", url or "", re.I) is not None


def _redirects_to_login(response: HttpResponse) -> bool:
    """True when an unfollowed 3xx answer points at the gateway login handshake."""
    if not 300 <= response.status_code < 400:
        return False
    location = response.headers.get("location")
    if isinstance(location, list):
        location = location[0] if location else None
    return bool(location) and _is_login_url(urljoin(response.url, location))


def router_sessions_full(html: str) -> bool:
    """True for the gateway's pool-full answer: the phrase on a login-shaped page. The same words inside
    a content page (a device name, a log line) are data, never a full session pool."""
    from .parser import looks_like_login

    return re.search(r"all web server sessions are in use", html, re.I) is not None and looks_like_login(html)


_TITLE_OPEN = re.compile(r"<title\b[^<>]*>", re.IGNORECASE)
_TITLE_CLOSE = re.compile(r"</title\s*>", re.IGNORECASE)


def title_of(html: str) -> str:
    opened = _TITLE_OPEN.search(html)
    if opened is None:
        return ""
    closed = _TITLE_CLOSE.search(html, opened.end())
    return html[opened.end() : closed.start()].strip() if closed else ""


def _headers_to_record(pairs: list[tuple[str, str]]) -> dict[str, str | list[str]]:
    record: dict[str, str | list[str]] = {}
    for name, value in pairs:
        key = name.lower()
        if key == "set-cookie":
            existing = record.get(key)
            record[key] = [*existing, value] if isinstance(existing, list) else [value]
        elif key in record and isinstance(record[key], str):
            record[key] = f"{record[key]}, {value}"
        else:
            record[key] = value
    return record


def _now_ms() -> int:
    """Monotonic milliseconds: only elapsed waits are measured, so wall-clock steps must not skew them."""
    return int(time.monotonic() * 1000)
