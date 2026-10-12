"""The real clients, one per rung. Each makes one request, never follows redirects (browsers
aside), and maps its client's errors onto FetchTransportError."""

import os
import ssl
import urllib.error
import urllib.request

import certifi
import httpx

from fetch_ladder.ladder import FetchFailed, FetchTransportError, Raw, RungUnavailable, Transport

UA = "Mozilla/5.0 (compatible; fetch-ladder/0.1; +https://github.com/jsnyde0)"
TIMEOUT_S = 30
FORM_HEADER = {"Content-Type": "application/x-www-form-urlencoded"}
# Karada's WAF answers 403 to httpx's default Accept-Encoding (br/zstd); gzip passes.
HTTPX_HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}


class _NoALPNContext(ssl.SSLContext):
    def set_alpn_protocols(self, protocols):  # linktr.ee refuses a Python hello that advertises ALPN (403)
        pass


def no_alpn_context() -> ssl.SSLContext:
    ctx = _NoALPNContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(certifi.where())
    return ctx


def _headers(mapping) -> dict[str, str]:
    return {k.lower(): v for k, v in mapping.items()}


def httpx_transport() -> Transport:
    client = httpx.Client(timeout=TIMEOUT_S, headers=HTTPX_HEADERS)

    def send(method, url, form):
        try:
            r = client.request(method, url, content=form, headers=FORM_HEADER if form is not None else None)
        except httpx.TransportError as e:
            raise FetchTransportError(f"{type(e).__name__}: {e}") from e
        return Raw(r.status_code, _headers(r.headers), r.text)

    return send


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx surfaces as HTTPError; the session vets and follows the hop


def _decode(data: bytes, headers) -> str:
    return data.decode(headers.get_content_charset() or "utf-8", errors="replace")


def urllib_transport() -> Transport:
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=no_alpn_context()), _NoRedirect)

    def send(method, url, form):
        headers = {"User-Agent": UA, **(FORM_HEADER if form is not None else {})}
        req = urllib.request.Request(url, data=form.encode() if form is not None else None, headers=headers, method=method)
        try:
            with opener.open(req, timeout=TIMEOUT_S) as r:
                return Raw(r.status, _headers(r.headers), _decode(r.read(), r.headers))
        except urllib.error.HTTPError as e:  # any non-2xx answer is an HTTP answer, not a transport error
            return Raw(e.code, _headers(e.headers), _decode(e.read(), e.headers))
        except (urllib.error.URLError, OSError) as e:
            raise FetchTransportError(f"{type(e).__name__}: {e}") from e

    return send


def curl_cffi_transport(target: str) -> Transport:
    from curl_cffi import requests as cffi

    session = cffi.Session()

    def send(method, url, form):
        try:
            r = session.request(
                method, url, data=form, impersonate=target, allow_redirects=False, timeout=TIMEOUT_S,
                headers=FORM_HEADER if form is not None else None,
            )  # fmt: skip
        except cffi.exceptions.RequestException as e:
            raise FetchTransportError(f"{type(e).__name__}: {e}") from e
        return Raw(r.status_code, _headers(r.headers), r.text)

    return send


def _key(var: str, rung: str) -> str:
    key = os.environ.get(var, "").strip()
    if not key:
        raise RungUnavailable(f"{rung}: rung unavailable, {var} is not set")
    return key


def firecrawl_transport() -> Transport:
    """The target's status rides in data.metadata.statusCode; without it the answer is not trusted."""
    key = _key("FIRECRAWL_API_KEY", "hosted:firecrawl")
    client = httpx.Client(timeout=TIMEOUT_S * 2)

    def send(method, url, form):
        if form is not None:
            raise FetchFailed("hosted:firecrawl reads pages by GET only", "unavailable")
        try:
            r = client.post(
                "https://api.firecrawl.dev/v1/scrape", headers={"Authorization": f"Bearer {key}"},
                json={"url": url, "formats": ["html"], "maxAge": 0},  # maxAge 0: never served from its cache
            )  # fmt: skip
        except httpx.TransportError as e:
            raise FetchTransportError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            return Raw(r.status_code, _headers(r.headers), r.text)
        try:
            data = r.json()["data"]
            html = data["html"]
        except (ValueError, KeyError, TypeError) as e:
            raise FetchFailed(f"hosted:firecrawl: unexpected response shape ({e!r})", "reader") from e
        status = (data.get("metadata") or {}).get("statusCode")
        return Raw(200, _headers(r.headers), html, target_status=status if isinstance(status, int) else None)

    return send


def jina_transport() -> Transport:
    """JSON mode carries the target's status in data.httpStatus; without it the answer is not trusted."""
    key = os.environ.get("JINA_API_KEY", "").strip()  # optional: keyless works at a lower rate limit
    client = httpx.Client(timeout=TIMEOUT_S * 2)

    def send(method, url, form):
        if form is not None:
            raise FetchFailed("hosted:jina reads pages by GET only", "unavailable")
        try:
            r = client.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "application/json", "X-No-Cache": "true", **({"Authorization": f"Bearer {key}"} if key else {})},
            )
        except httpx.TransportError as e:
            raise FetchTransportError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            return Raw(r.status_code, _headers(r.headers), r.text)
        try:
            data = r.json()["data"]
            content = data["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise FetchFailed(f"hosted:jina: unexpected response shape ({e!r})", "reader") from e
        status = data.get("httpStatus")
        return Raw(200, _headers(r.headers), content, target_status=status if isinstance(status, int) else None)

    return send


def build(rung: str) -> Transport:
    if rung == "httpx":
        return httpx_transport()
    if rung == "urllib":
        return urllib_transport()
    if rung.startswith("curl_cffi:") and rung != "curl_cffi:":
        return curl_cffi_transport(rung.split(":", 1)[1])
    if rung == "playwright":
        raise RungUnavailable("playwright: rung not built")
    if rung == "hosted:jina":
        return jina_transport()
    if rung == "hosted:firecrawl":
        return firecrawl_transport()
    raise FetchFailed(f"unknown rung {rung!r}", "unavailable")
