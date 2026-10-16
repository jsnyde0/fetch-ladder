"""The fetch ladder: one Session, rungs from honest clients up to hosted readers, shared manners.

`fetch(url)` fetches by site: a host the site memory knows uses its saved rung; an unknown host
climbs, and so does a saved rung that meets a block (a bot check, 401/403, or a transport error
that survives two retries on the rung). The winner is saved. Every other failure fails loud
without climbing: other statuses (a dead link), vetting, a target's 429, a spent hosted cap.

Manners live here, not in the transports: per-host pacing (across processes, through the day
ledger), a target's HTTP 429 wait-out (then the host is blocked for the session, every rung),
challenge pages as failures, vetting of every hop (scheme, reader/proxy shapes, private
addresses), the hosted cap, readers stopped for the day on 402/429, and credential-shaped links
kept off hosted readers. A transport is a callable `(method, url, form) -> Raw` that makes ONE
request and does not follow redirects (a browser rung may, and reports Raw.final_url); it also takes `timeout=` when a fetch has a deadline.
"""

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse

from bs4 import BeautifulSoup

from fetch_ladder import store as stores

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
HOSTED_CAP = 30  # hosted calls per UTC day per machine (fits Firecrawl's 1000 free a month)
NIGHTLY_RESERVE = 20  # of HOSTED_CAP: only caller='nightly' may spend these; ad-hoc callers stop at cap - reserve
CALLERS = ("adhoc", "nightly")
TRANSPORT_RETRIES = 2  # ADR-008 D4, on the same rung, before a by-site fetch climbs
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

# Links that are themselves an access credential never go to a hosted reader (switch-berlin
# sb-7wzb.103 D4: this exact list; adding a shape is an edit to that decision).
CREDENTIAL_HOSTS = (
    "t.me", "telegram.me", "chat.whatsapp.com", "signal.group", "discord.gg", "forms.gle", "drive.google.com",
)  # fmt: skip
CREDENTIAL_PATHS = (("discord.com", "/invite/"), ("docs.google.com", "/forms/"))
CREDENTIAL_KEYS = ("token", "key", "code", "invite", "secret", "sig", "signature", "access", "auth")


class FetchFailed(Exception):
    """The fetch got no usable page. `kind`: status, challenge, rate_limited, vetting, redirects,
    refused (hosted not allowed for this call or link), unavailable, reader (a hosted reader's own
    failure, or a reader hiding the target status), reader_stopped (402/429 from a reader: stopped
    for the day), cap (hosted cap spent), exhausted (every rung failed), deadline (the caller's deadline passed). `status` is the target's
    HTTP status, if there was one."""

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
    target_status: int | None = None  # a hosted reader's report of the target's status; None: hidden


@dataclass(frozen=True)
class Page:
    body: str
    text: str
    final_url: str
    status: int
    links: tuple[str, ...]
    rung: str


Transport = Callable[..., Raw]  # (method, url, form, *, timeout=None); a deadline passes `timeout`
Resolve = Callable[[str], list[str]]


def resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _private(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast
        or addr.is_unspecified
    )  # fmt: skip


def vet(url: str, resolve: Resolve = resolve_host) -> None:
    """One hop: http(s) only, no reader/proxy shapes, and every address the host resolves to is
    public (an unresolvable host fails here too). The resolve-then-connect window stays open."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchFailed(f"{url!r}: only http(s) URLs", "vetting")
    host = parsed.hostname.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in READER_HOSTS):
        raise FetchFailed(f"{url!r}: {host} is a reader/proxy host", "vetting")
    if EMBEDDED_URL.search(parsed.path) or EMBEDDED_URL.search(parsed.query):
        raise FetchFailed(f"{url!r}: a URL inside the path or query looks like a reader proxy", "vetting")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = resolve(host)
        except OSError as e:
            raise FetchFailed(f"{url!r}: {host} does not resolve ({e})", "vetting") from e
    for ip in addresses:
        if _private(ip):
            raise FetchFailed(f"{url!r}: {host} resolves to a private address ({ip})", "vetting")


def credential_shaped(url: str) -> bool:
    """A link that is itself an access credential (an invite, a form, a signed URL)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in CREDENTIAL_HOSTS:
        return True
    if any(host == h and parsed.path.startswith(p) for h, p in CREDENTIAL_PATHS):
        return True
    return any(k.lower() in CREDENTIAL_KEYS for k, _ in parse_qsl(parsed.query, keep_blank_values=True))


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _retry_after(raw: Raw) -> float:
    value = raw.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else RATE_LIMIT_PAUSE_S


def _readable(html: str, base: str) -> tuple[str, tuple[str, ...]]:
    soup = BeautifulSoup(html, "html.parser")
    links = dict.fromkeys(urljoin(base, a["href"]) for a in soup.find_all("a", href=True))
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True), tuple(link for link in links if link.startswith(("http://", "https://")))


def _block_shaped(e: Exception) -> bool:
    if isinstance(e, FetchTransportError):
        return True
    return isinstance(e, FetchFailed) and (e.kind == "challenge" or (e.kind == "status" and e.status in (401, 403)))


class Session:
    """One politeness state across rungs.

    `store`: one store folder, read and written alone (where no git toplevel exists); default:
    the project store at the git toplevel of the working directory if opted in, then the global
    store. `home`: the global home (store and day ledger); default FETCH_LADDER_HOME or
    ~/.fetch-ladder. `allow_hosted=False` keeps every call on local rungs. `transports` overrides
    rungs by name and `resolve` the address lookup (tests). `clock` is wall time: the ledger
    shares it across processes. `caller`: 'adhoc' (default) or 'nightly'; of `cap` hosted calls a day,
    the last `nightly_reserve` are the nightly's alone. `monotonic` is what a fetch `deadline` is measured on."""

    def __init__(
        self, *, allow_hosted=True, gap_s=GAP_S, cap=HOSTED_CAP, nightly_reserve=NIGHTLY_RESERVE, caller="adhoc", store: Path | None = None, home: Path | None = None,
        transports=None, resolve: Resolve = resolve_host, sleep=time.sleep, clock=time.time, monotonic=time.monotonic,
    ):  # fmt: skip
        self.allow_hosted = allow_hosted
        self.gap_s = gap_s
        if caller not in CALLERS:
            raise ValueError(f"caller must be one of {CALLERS}, got {caller!r}")
        self.cap = cap
        # ad-hoc callers (the default) stop `nightly_reserve` short of the cap; the nightly may spend it all
        self.limit = cap if caller == "nightly" else max(cap - nightly_reserve, 0)
        self.home = Path(home) if home is not None else stores.home()
        if store is not None:
            self._stores = [stores.SiteStore(store)]
        else:
            project = stores.project_store()
            self._stores = [stores.SiteStore(f) for f in ((project,) if project else ()) + (self.home,)]
        self._transports: dict[str, Transport] = dict(transports or {})
        self._resolve = resolve
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._deadline: float | None = None
        self._blocked: set[str] = set()

    @property
    def ledger(self) -> stores.Ledger:
        return stores.Ledger(self.home, stores.utc_day(self._clock()))

    def saved(self, host: str) -> dict | None:
        """The host's entry in the first store that knows it."""
        for s in self._stores:
            entry = s.get(host)
            if entry is not None:
                return entry
        return None

    def _transport(self, rung: str) -> Transport:
        if rung not in self._transports:
            from fetch_ladder import transports

            self._transports[rung] = transports.build(rung)  # raises RungUnavailable / FetchFailed
        return self._transports[rung]

    def fetch(self, url: str, rung: str | None = None, form: str | None = None, note: Callable[[str], None] = lambda _: None, deadline: float | None = None) -> Page:
        """GET `url` (POST `form`, a URL-encoded body, if given). With `rung`, through that rung
        only, never writing the store; without, by site (see the module doc). `deadline` is an
        absolute `monotonic()` timestamp: once it passes no rung, retry, or pacing wait starts
        (FetchFailed, kind deadline), and each transport call is given the time left as its timeout."""
        self._deadline = deadline
        try:
            return self._fetch_by(url, rung, form, note)
        finally:
            self._deadline = None

    def _fetch_by(self, url: str, rung: str | None, form: str | None, note: Callable[[str], None]) -> Page:
        if rung is not None:
            return self._fetch(url, rung, form)
        entry = self.saved(_host(url))
        if entry is None:
            return self.climb(url, form, note)
        try:
            return self._fetch_retrying(url, entry["rung"], form, note)
        except (FetchFailed, FetchTransportError) as e:
            if not _block_shaped(e):
                raise
            note(f"saved rung {entry['rung']} blocked: {e}")
            return self.climb(url, form, note, skip=entry["rung"], tried=[f"{entry['rung']}: {e}"])

    def climb(self, url: str, form: str | None = None, note: Callable[[str], None] = lambda _: None, skip: str | None = None, tried: list[str] | None = None) -> Page:
        """Each rung in CLIMB order until a usable body; the winner is saved. Unbuilt and keyless
        rungs, stopped readers, and hosted rungs for a credential-shaped link or without
        allow_hosted are skipped (said via `note`). A failure that is not block-shaped ends the
        climb. If every rung fails, FetchFailed (kind exhausted) names each, the `skip`ped saved rung's
        own failure (passed in `tried`) first."""
        tried = list(tried or [])
        dead_local = blocked_local = 0  # local rungs that failed with no HTTP answer / with an answer
        for rung in CLIMB:
            if rung == skip:
                continue
            self._check_deadline(url)
            if rung.startswith("hosted:"):
                reason = self._hosted_refusal(url) or (self.ledger.stopped(rung) and "stopped for the day")
                if reason:
                    note(f"skip {rung}: {reason}")
                    continue
                if dead_local >= 2 and not blocked_local:  # dead network (ADR-008 D4): a hosted reader cannot help
                    raise FetchFailed(f"{url}: network failure on every local rung: " + "; ".join(tried), "network")
            try:
                page = self._fetch_retrying(url, rung, form, note)
            except RungUnavailable as e:
                note(f"skip {rung}: {e}")
                continue
            except (FetchFailed, FetchTransportError) as e:
                if not (_block_shaped(e) or (isinstance(e, FetchFailed) and e.kind in ("reader", "reader_stopped"))):
                    raise
                note(f"{rung} failed: {e}")
                if not rung.startswith("hosted:"):
                    if isinstance(e, FetchTransportError):
                        dead_local += 1
                    else:
                        blocked_local += 1
                tried.append(f"{rung}: {e}")
                continue
            self._save(_host(url), rung)
            return page
        if not tried:
            raise FetchFailed(f"{url}: no rung could run", "unavailable")
        raise FetchFailed(f"{url}: every rung failed: " + "; ".join(tried), "exhausted")

    def _save(self, host: str, rung: str) -> None:
        old = (self.saved(host) or {}).get("rung")
        if old == rung:
            return
        target = self._stores[0]
        target.put(host, rung=rung, won=stores.utc_day(self._clock()))
        self.ledger.rung_change(host, old, rung, target.folder)

    def _hosted_refusal(self, url: str) -> str | None:
        if not self.allow_hosted:
            return "hosted rungs are off for this session"
        if credential_shaped(url):
            return "a credential-shaped link stays on local rungs"
        return None

    def _fetch_retrying(self, url: str, rung: str, form: str | None, note: Callable[[str], None]) -> Page:
        for attempt in range(TRANSPORT_RETRIES + 1):
            self._check_deadline(url)
            try:
                return self._fetch(url, rung, form)
            except FetchTransportError as e:
                if attempt == TRANSPORT_RETRIES:
                    raise
                note(f"{rung} transport error, retrying: {e}")
        raise AssertionError("unreachable")

    def _fetch(self, url: str, rung: str, form: str | None) -> Page:
        hosted = rung.startswith("hosted:")
        if hosted:
            reason = self._hosted_refusal(url)
            if reason:
                raise FetchFailed(f"{rung}: {reason}", "refused")
        transport = self._transport(rung)
        vet(url, self._resolve)
        if hosted:
            return self._fetch_hosted(transport, rung, url, form)
        current, method = url, "POST" if form is not None else "GET"
        for _ in range(MAX_REDIRECTS + 1):
            vet(current, self._resolve)  # every hop the client itself sees
            raw = self._send(transport, method, current, form)
            if raw.final_url:
                vet(raw.final_url, self._resolve)  # a browser's landing URL
                current = raw.final_url
            location = raw.headers.get("location")
            if 300 <= raw.status < 400 and location:
                current = urljoin(current, location)
                if raw.status not in (307, 308):
                    method, form = "GET", None
                continue
            break
        else:
            raise FetchFailed(f"{url}: more than {MAX_REDIRECTS} redirects", "redirects")
        return self._page(raw, raw.status, current, rung)

    def _fetch_hosted(self, transport: Transport, rung: str, url: str, form: str | None) -> Page:
        """A hosted reader is handed the target only. Its 402/429 stops the reader for the day,
        never the target host; a response that hides the target's status is a failure."""
        if self.ledger.stopped(rung):
            raise FetchFailed(f"{rung}: stopped for the day", "reader_stopped")
        if not self.ledger.take_hosted(rung, self.limit):
            raise FetchFailed(f"{rung}: hosted cap of {self.limit} calls today is spent for this caller", "cap")
        self._pace(_host(url))
        try:
            raw = self._call(transport, "POST" if form is not None else "GET", url, form)
        except FetchTransportError:
            self.ledger.refund_hosted(rung)  # no HTTP answer: the call did not spend the day's cap
            raise
        if raw.status in (402, 429):
            self.ledger.stop(rung)
            raise FetchFailed(f"{rung}: reader answered HTTP {raw.status}; stopped for the day", "reader_stopped")
        if raw.status != 200:
            raise FetchFailed(f"{rung}: reader answered HTTP {raw.status}", "reader")
        if raw.target_status is None:
            raise FetchFailed(f"{rung}: the reader's response hides the target status", "reader")
        if raw.final_url:
            vet(raw.final_url, self._resolve)
        return self._page(raw, raw.target_status, raw.final_url or url, rung)

    def _page(self, raw: Raw, status: int, url: str, rung: str) -> Page:
        if CHALLENGE.search(raw.text[:5000]):
            raise FetchFailed(f"{rung}: bot check (HTTP {status})", "challenge", status)
        if not 200 <= status < 300:
            raise FetchFailed(f"{rung}: HTTP {status} for {url}", "status", status)
        text, links = _readable(raw.text, url)
        return Page(raw.text, text, url, status, links, rung)

    def _gap(self, host: str) -> float:
        entry = self.saved(host) or {}
        return max(self.gap_s, entry.get("gap_s", 0.0))

    def _check_deadline(self, url: str, wait: float = 0.0) -> None:
        if self._deadline is not None and self._monotonic() + wait >= self._deadline:
            raise FetchFailed(f"{url}: fetch deadline passed", "deadline")

    def _pause(self, url: str, seconds: float) -> None:
        self._check_deadline(url, seconds)  # a wait that would cross the deadline is not started
        self._sleep(seconds)

    def _call(self, transport: Transport, method: str, url: str, form: str | None) -> Raw:
        """One transport request, given the time left to the deadline as its timeout."""
        if self._deadline is None:
            return transport(method, url, form)
        self._check_deadline(url)
        return transport(method, url, form, timeout=self._deadline - self._monotonic())

    def _pace(self, host: str) -> None:
        now = self._clock()
        wait = self.ledger.reserve(host, self._gap(host), now) - now
        if wait > 0:
            self._pause(host, wait)

    def _send(self, transport: Transport, method: str, url: str, form: str | None) -> Raw:
        host = _host(url)
        if host in self._blocked:
            raise FetchFailed(f"{host} rate limited this session (HTTP 429)", "rate_limited", 429)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._pace(host)
            raw = self._call(transport, method, url, form)
            if raw.status != 429:
                return raw
            pause = _retry_after(raw)
            if attempt == RATE_LIMIT_RETRIES or pause > RETRY_AFTER_CAP_S:
                break
            self._pause(host, pause)
        self._blocked.add(host)
        raise FetchFailed(f"{host} rate limited this session (HTTP 429)", "rate_limited", 429)
