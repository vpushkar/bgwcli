"""HTTP client for the BGW320 web UI (port of BGW320-CLI src/client.ts).

Transport is urllib.request with redirects never followed automatically: GET requests are
followed manually (max 5 hops) exactly like the TypeScript client, POSTs are not, so callers
such as restore can observe the router's ``302 Location: /cgi-bin/home.ha`` answer.

Cookies live in a plain dict in insertion order so the shared session JSON
(``RouterSessionSnapshot``) round-trips byte-for-byte with the TypeScript CLI.
"""

from __future__ import annotations

import hashlib
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit

from .config import GlobalOptions
from .errors import BgwError, RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from .types import HttpMethod, HttpResponse, RouterSessionSnapshot

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_USER_AGENT = "bgw/0.1.0"
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_DEFAULT_PORTS = {"http": 80, "https": 443}

_sleep = time.sleep


class RouterResponseError(BgwError):
    """The router answered with an HTTP error status or an oversized body (exit 1, like TS)."""


def session_pool_full_error(
    message: str = "Router web session pool is full.", *, waited_ms: int = 0, retry_count: int = 0
) -> RouterSessionPoolFullError:
    """Build the shared RouterSessionPoolFullError carrying the TS ``waitedMs`` / ``retryCount`` metadata."""
    error = RouterSessionPoolFullError(message)
    error.session_pool_full = True  # type: ignore[attr-defined]
    error.waited_ms = waited_ms  # type: ignore[attr-defined]
    error.retry_count = retry_count  # type: ignore[attr-defined]
    return error


def pool_full_metadata(error: BaseException) -> tuple[int, int]:
    """(waited_ms, retry_count) of a RouterSessionPoolFullError; zeros when absent."""
    return int(getattr(error, "waited_ms", 0) or 0), int(getattr(error, "retry_count", 0) or 0)


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


class _PassThroughErrors(urllib.request.HTTPErrorProcessor):
    """Return every status as a response: no HTTPError, and therefore no automatic redirects."""

    def http_response(self, request, response):  # noqa: D401 - urllib hook
        return response

    https_response = http_response


def urllib_transport(request: HttpRequest) -> RawResponse:
    handlers: list[urllib.request.BaseHandler] = [_PassThroughErrors()]
    if urlsplit(request.url).scheme == "https":
        if request.insecure_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(request.url, data=request.body, method=request.method)
    for name, value in request.headers.items():
        req.add_header(name, value)
    # Total deadline, like the TS AbortController: urllib's timeout only bounds each socket
    # operation, so a router dribbling one byte at a time could otherwise hold the CLI forever.
    deadline = time.monotonic() + max(0.001, request.timeout_ms / 1000)
    remaining = lambda: deadline - time.monotonic()  # noqa: E731
    with opener.open(req, timeout=max(0.001, remaining())) as response:
        chunks: list[bytes] = []
        total = 0
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


class RouterPoster(PageGetter, Protocol):
    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse: ...


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
# Protected page used to verify a login that did not answer with the usual 302 -> home.ha.
_LOGIN_PROBE_PAGE = "services"
SLOW_PAGE_TIMEOUT_MS: dict[str, int] = {"home": 45000, "lanstatistics": 45000}
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
        for name, value in (cookies or {}).items():
            if name and value:
                self._cookies[str(name)] = str(value)
        self._authenticated = len(self._cookies) > 0

    def clear_session(self) -> None:
        self._cookies.clear()
        self._authenticated = False

    # -- pages -----------------------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        response = self.get_cgi_page("sitemap", auth=False)
        return {
            "host": urlsplit(self._origin).netloc,
            "reachable": 200 <= response.status_code < 500,
            "title": title_of(response.body),
            "authenticated": self._authenticated,
        }

    def get_cgi_page(self, page: str, *, auth: bool = True) -> HttpResponse:
        from .parser import looks_like_login

        path = f"/cgi-bin/{page}.ha"
        response = self._request(path, "GET")
        if not auth:
            return response
        if looks_like_login(response.body):
            self.login(response.body, force=True)
            retry = self._request(path, "GET")
            if looks_like_login(retry.body):
                self._authenticated = False
                raise RouterAuthError(
                    "Router returned the login page after authentication. "
                    "Wait for stale router web sessions to expire, then retry."
                )
            return retry
        return response

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse:
        """POST a page's own form: nonce from GET <page>.ha, POST to /cgi-bin/<page>.ha."""
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

        if not self.has_authenticated_session():
            self.login()
        response = self._post_form_once(nonce_page, post_path, fields)
        if looks_like_login(response.body):
            self.login(response.body, force=True)
            response = self._post_form_once(nonce_page, post_path, fields)
        if router_sessions_full(response.body):
            self._authenticated = False
            raise session_pool_full_error()
        if looks_like_login(response.body):
            self._authenticated = False
            raise RouterAuthError("Router returned the login page instead of accepting the operation.")
        if response.status_code >= 400:
            raise RouterResponseError(f"Router rejected {post_path} with HTTP {response.status_code}.")
        return response

    def _post_form_once(self, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> HttpResponse:
        current = self.get_cgi_page(nonce_page)
        path = f"/cgi-bin/{post_path}"
        # A page can host several forms, each with its own nonce (home.ha's Restart forms carry two).
        # Use the nonces of the form whose action is the target path; fall back to the page's first
        # nonce for single-form pages. Posting another form's nonce is rejected by the gateway.
        nonces = form_nonces(current.body, path)
        if not nonces:
            page_nonce = extract_nonce(current.body)
            nonces = [page_nonce] if page_nonce else []
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
            nonce = extract_nonce(login_page.body)
            if not nonce:
                _sleep((150 + attempt * 100) / 1000)
                login_page = self._request("/cgi-bin/login.ha", "GET")
                if router_sessions_full(login_page.body):
                    nonce = self._wait_for_session_nonce()
                    break
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
        probe = self._request(f"/cgi-bin/{_LOGIN_PROBE_PAGE}.ha", "GET")
        if looks_like_login(probe.body):
            self._authenticated = False
            raise RouterAuthError("Login failed. Check the device access code.")
        self._authenticated = True

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
            return extract_nonce(login_page.body)
        raise session_pool_full_error(waited_ms=timeout_ms, retry_count=retry_count)

    # -- transport -------------------------------------------------------------------------------

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
            raw = self._transport(request)
        except BgwError:
            raise
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
            pair = cookie.split(";")[0]
            parts = pair.split("=")
            name = parts[0]
            value = parts[1] if len(parts) > 1 else ""
            if name and value:
                self._cookies[name] = value


# --- helpers ----------------------------------------------------------------------------------------


def normalize_origin(host: str) -> str:
    """``new URL(host).origin`` semantics: scheme://lowercase-host[:non-default-port]; https assumed."""
    clean = re.sub(r"/+$", "", host)
    if not re.match(r"^https?://", clean, re.I):
        clean = f"https://{clean}"
    parts = urlsplit(clean)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"


_FORM = re.compile(r"<form\b[^>]*>.*?</form>", re.IGNORECASE | re.DOTALL)
_FORM_ACTION = re.compile(r"""\baction\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_NONCE_INPUT = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*["']nonce["'][^>]*\bvalue\s*=\s*["']([a-f0-9]+)["']""", re.IGNORECASE
)


def form_nonces(html: str, action_path: str) -> list[str]:
    """Nonce values (in document order) of the form whose action equals ``action_path``
    (e.g. ``/cgi-bin/wrestart.ha?1``); empty when no such form exists."""
    for form in _FORM.finditer(html):
        action = _FORM_ACTION.search(form.group(0))
        if action and action.group(1).strip() == action_path:
            return _NONCE_INPUT.findall(form.group(0))
    return []


def extract_nonce(html: str) -> str | None:
    direct = re.search(r"name=[\"']nonce[\"'][^>]*value=[\"']([a-f0-9]+)[\"']", html, re.I)
    if direct:
        return direct.group(1)
    reverse = re.search(r"value=[\"']([a-f0-9]+)[\"'][^>]*name=[\"']nonce[\"']", html, re.I)
    return reverse.group(1) if reverse else None


def router_sessions_full(html: str) -> bool:
    return re.search(r"all web server sessions are in use", html, re.I) is not None


def title_of(html: str) -> str:
    match = re.search(r"<title\b[^>]*>([\s\S]*?)</title>", html, re.I)
    return match.group(1).strip() if match else ""


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
    return int(time.time() * 1000)
