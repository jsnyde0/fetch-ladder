"""The fetch ladder: one Session, rungs from honest clients up to hosted readers, shared manners.

Manners live here, not in the transports: per-host pacing, HTTP 429 wait-out (then the host is
blocked for the session, every rung), challenge pages as failures, URL vetting. A transport is a
callable `(method, url, form) -> Raw` that makes ONE request and does not follow redirects (a
browser rung may, and reports Raw.final_url). Transports map their client's errors onto the two
exception kinds. Status errors never retry here; transport errors are the consumer's to retry.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

CLIMB = (
    "httpx",
    "urllib",
    "curl_cffi:chrome",
    "curl_cffi:firefox",
    "curl_cffi:safari",
    "playwright",
    "hosted:jina",
    "hosted:firecrawl",
)
GAP_S = 1.0
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_PAUSE_S = 30.0
RETRY_AFTER_CAP_S = 120.0
MAX_REDIRECTS = 6

READER_HOSTS = (
    "jina.ai", "firecrawl.dev", "firecrawl.com", "pure.md", "12ft.io", "archive.ph", "archive.today",
    "archive.is", "archive.org", "translate.goog",
)  # fmt: skip
EMBEDDED_URL = re.compile(r"https?(://|:/|%3a)", re.IGNORECASE)  # a URL inside a URL is a reader-proxy shape
CHALLENGE = re.compile(
    r"checking your browser|just a moment\.\.\.|attention required|enable javascript and cookies"
    r"|cf-chl|ddos protection",
    re.IGNORECASE,
)


class FetchFailed(Exception):
    """The fetch got no usable page. `kind`: status, challenge, rate_limited, vetting, redirects,
    refused (hosted without allow_hosted), unavailable. `status` is the HTTP status, if there was one."""

    def __init__(self, message: str, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class RungUnavailable(FetchFailed):
    """The rung cannot run here: not built, or its key is missing."""

    def __init__(self, message: str):
        super().__init__(message, "unavailable")


class FetchTransportError(Exception):
    """Connect, TLS, timeout or reset: no HTTP answer."""


@dataclass(frozen=True)
class Raw:
    status: int
    headers: dict[str, str]  # lower-case names
    text: str
    final_url: str | None = None  # set by a transport that follows redirects itself (a browser)


@dataclass(frozen=True)
class Page:
    body: str
    text: str
    final_url: str
    status: int
    links: tuple[str, ...]
    rung: str


Transport = Callable[[str, str, str | None], Raw]


def vet(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchFailed(f"{url!r}: only http(s) URLs", "vetting")
    host = parsed.hostname.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in READER_HOSTS):
        raise FetchFailed(f"{url!r}: {host} is a reader/proxy host", "vetting")
    if EMBEDDED_URL.search(parsed.path) or EMBEDDED_URL.search(parsed.query):
        raise FetchFailed(f"{url!r}: a URL inside the path or query looks like a reader proxy", "vetting")


def _retry_after(raw: Raw) -> float:
    value = raw.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else RATE_LIMIT_PAUSE_S


def _readable(html: str, base: str) -> tuple[str, tuple[str, ...]]:
    soup = BeautifulSoup(html, "html.parser")
    links = dict.fromkeys(urljoin(base, a["href"]) for a in soup.find_all("a", href=True))
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True), tuple(link for link in links if link.startswith(("http://", "https://")))


class Session:
    """One politeness state across rungs. `transports` overrides rungs by name (tests)."""

    def __init__(self, *, allow_hosted=False, gap_s=GAP_S, transports=None, sleep=time.sleep, clock=time.monotonic):
        self.allow_hosted = allow_hosted
        self.gap_s = gap_s
        self._transports: dict[str, Transport] = dict(transports or {})
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._blocked: set[str] = set()

    def _transport(self, rung: str) -> Transport:
        if rung not in self._transports:
            from fetch_ladder import transports

            self._transports[rung] = transports.build(rung)  # raises RungUnavailable / FetchFailed
        return self._transports[rung]

    def fetch(self, url: str, rung: str = "httpx", form: str | None = None) -> Page:
        """GET `url` (POST `form`, a URL-encoded body, if given) through one rung."""
        hosted = rung.startswith("hosted:")
        if hosted and not self.allow_hosted:
            raise FetchFailed(f"{rung}: hosted rungs need allow_hosted", "refused")
        transport = self._transport(rung)
        vet(url)
        current, method = url, "POST" if form is not None else "GET"
        for _ in range(MAX_REDIRECTS + 1):
            vet(current)  # every hop the client itself sees; a hosted reader is handed the target only
            raw = self._send(transport, method, current, form)
            if raw.final_url:
                vet(raw.final_url)  # a browser's landing URL
                current = raw.final_url
            location = raw.headers.get("location")
            if 300 <= raw.status < 400 and location and not hosted:
                current = urljoin(current, location)
                if raw.status not in (307, 308):
                    method, form = "GET", None
                continue
            break
        else:
            raise FetchFailed(f"{url}: more than {MAX_REDIRECTS} redirects", "redirects")
        if CHALLENGE.search(raw.text[:5000]):
            raise FetchFailed(f"{rung}: bot check (HTTP {raw.status})", "challenge", raw.status)
        if raw.status >= 400:
            raise FetchFailed(f"{rung}: HTTP {raw.status} for {current}", "status", raw.status)
        text, links = _readable(raw.text, current)
        return Page(raw.text, text, current, raw.status, links, rung)

    def _send(self, transport: Transport, method: str, url: str, form: str | None) -> Raw:
        host = urlparse(url).netloc
        if host in self._blocked:
            raise FetchFailed(f"{host} rate limited this session (HTTP 429)", "rate_limited", 429)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            wait = self._last.get(host, float("-inf")) + self.gap_s - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last[host] = self._clock()
            raw = transport(method, url, form)
            if raw.status != 429:
                return raw
            pause = _retry_after(raw)
            if attempt == RATE_LIMIT_RETRIES or pause > RETRY_AFTER_CAP_S:
                break
            self._sleep(pause)
        self._blocked.add(host)
        raise FetchFailed(f"{host} rate limited this session (HTTP 429)", "rate_limited", 429)

    def climb(self, url: str, form: str | None = None, note: Callable[[str], None] = lambda _: None) -> Page:
        """One request per rung in CLIMB order until a usable body. Unbuilt and keyless rungs are
        skipped (said via `note`); hosted rungs are left out unless allow_hosted. A rate limit or a
        vetting failure ends the climb. If every rung fails, the last failure is raised."""
        last: Exception | None = None
        for rung in CLIMB:
            if rung.startswith("hosted:") and not self.allow_hosted:
                continue
            try:
                return self.fetch(url, rung, form)
            except RungUnavailable as e:
                note(f"skip {rung}: {e}")
            except FetchFailed as e:
                if e.kind in ("rate_limited", "vetting"):
                    raise
                note(f"{rung} failed: {e}")
                last = e
            except FetchTransportError as e:
                note(f"{rung} transport error: {e}")
                last = e
        if last is None:
            raise FetchFailed("no rung could run", "unavailable")
        raise last
