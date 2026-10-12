"""Fetch by site on fakes: the site memory, climbing, per-hop address vetting, the hosted cap and
reader stop, the credential-link exclusion. No live network; no hosted reader is ever called."""

import json
import multiprocessing

import pytest

from fetch_ladder import FetchFailed, FetchTransportError, Raw, Session, credential_shaped
from fetch_ladder.store import Ledger, SiteStore, StoreError, init

OK = Raw(200, {}, "<html><body><p>hello</p></body></html>")
CHALLENGE = Raw(200, {}, "<title>Just a moment...</title>")
LOCAL = ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")
HOSTED = ("hosted:jina", "hosted:firecrawl")
DAY = "1970-01-01"  # the test clock starts at epoch 0


def hosted_ok(status=200):
    return Raw(200, {}, "<p>via reader</p>", target_status=status)


class Fake:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, form):
        self.calls.append((method, url, form))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def __call__(self):
        return self.now


PUBLIC = {"a.test": ["93.184.216.34"], "b.test": ["93.184.216.35"]}


def resolver(table=None):
    table = {**PUBLIC, **(table or {})}

    def resolve(host):
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]

    return resolve


@pytest.fixture
def env(tmp_path):
    store = init(tmp_path / "proj" / ".fetch-ladder")
    home = tmp_path / "home"

    def make(transports, resolve=None, **kw):
        clock = Clock()
        s = Session(
            transports=transports, sleep=clock.sleep, clock=clock, resolve=resolve or resolver(),
            store=store, home=home, **kw,
        )  # fmt: skip
        s.test_clock = clock
        return s

    make.store = SiteStore(store)
    make.ledger = Ledger(home, DAY)
    return make


def blocked_below(overrides=None):
    """Every local rung answers with a bot check, unless overridden."""
    return {**{r: Fake(CHALLENGE) for r in LOCAL}, **(overrides or {})}


# (i) unknown host bot-checked on httpx, served on curl_cffi -> page returned, store saves curl_cffi
def test_unknown_host_climbs_and_saves_winner(env):
    fs = {"httpx": Fake(CHALLENGE), "urllib": Fake(Raw(403, {}, "no")), "curl_cffi:chrome": Fake(OK)}
    page = env(fs).fetch("https://a.test/p")
    assert page.rung == "curl_cffi:chrome" and "hello" in page.text
    assert env.store.get("a.test") == {"rung": "curl_cffi:chrome", "won": DAY}
    assert env.ledger.read()["rung_changes"] == [
        {"host": "a.test", "from": None, "to": "curl_cffi:chrome", "store": str(env.store.folder)}
    ]


def test_known_host_uses_saved_rung_only(env):
    env.store.put("a.test", rung="urllib", won="2026-10-01")
    h, u = Fake(OK), Fake(OK)
    page = env({"httpx": h, "urllib": u}).fetch("https://a.test/p")
    assert page.rung == "urllib" and not h.calls and len(u.calls) == 1
    assert env.store.get("a.test") == {"rung": "urllib", "won": "2026-10-01"}  # success alone writes nothing
    assert env.ledger.read()["rung_changes"] == []


# (ii) 404 on httpx -> FetchFailed, no further rung tried, store unchanged
@pytest.mark.parametrize("status", (404, 410, 500))
def test_dead_link_fails_without_climbing(env, status):
    h, u = Fake(Raw(status, {}, "gone")), Fake(OK)
    with pytest.raises(FetchFailed) as e:
        env({"httpx": h, "urllib": u}).fetch("https://a.test/gone")
    assert e.value.kind == "status" and e.value.status == status and not u.calls
    assert env.store.hosts() == {}


# (iii) hosted reader 200 wrapping target 404 -> failure, not saved
def test_hosted_wrapping_target_404_fails_and_is_not_saved(env):
    fs = blocked_below({"hosted:jina": Fake(hosted_ok(404)), "hosted:firecrawl": Fake(hosted_ok())})
    with pytest.raises(FetchFailed) as e:
        env(fs).fetch("https://a.test/p")
    assert e.value.status == 404 and env.store.hosts() == {}
    assert not fs["hosted:firecrawl"].calls  # a dead target is not climbed past


def test_hosted_winner_with_visible_target_status_is_saved(env):
    fs = blocked_below({"hosted:jina": Fake(hosted_ok())})
    assert env(fs).fetch("https://a.test/p").rung == "hosted:jina"
    assert env.store.get("a.test")["rung"] == "hosted:jina"
    assert env.ledger.read()["hosted"] == {"hosted:jina": 1}


# (ix) hosted reader whose response hides the target status -> failure, not saved
def test_hosted_hiding_target_status_fails_closed(env):
    fs = blocked_below({"hosted:jina": Fake(Raw(200, {}, "<p>x</p>")), "hosted:firecrawl": Fake(hosted_ok())})
    page = env(fs).fetch("https://a.test/p")
    assert page.rung == "hosted:firecrawl" and env.store.get("a.test")["rung"] == "hosted:firecrawl"
    with pytest.raises(FetchFailed) as e:
        env({"hosted:jina": Fake(Raw(200, {}, "<p>x</p>"))}).fetch("https://b.test/", rung="hosted:jina")
    assert e.value.kind == "reader" and "target status" in str(e.value)


# (iv) private addresses on every hop of every local rung
PRIVATE = ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/", "http://[::1]/",
           "http://192.168.0.1/", "http://0.0.0.0/")  # fmt: skip


@pytest.mark.parametrize("target", PRIVATE)
@pytest.mark.parametrize("rung", LOCAL)
def test_redirect_to_private_address_is_vetting_failure(env, rung, target):
    f = Fake(Raw(302, {"location": target}, ""))
    with pytest.raises(FetchFailed) as e:
        env({rung: f}).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "vetting" and len(f.calls) == 1


@pytest.mark.parametrize("rung", LOCAL)
def test_hostname_resolving_private_is_vetting_failure(env, rung):
    f = Fake(OK)
    with pytest.raises(FetchFailed) as e:
        env({rung: f}, resolve=resolver({"evil.test": ["93.184.216.40", "10.0.0.5"]})).fetch("https://evil.test/", rung=rung)
    assert e.value.kind == "vetting" and "10.0.0.5" in str(e.value) and not f.calls


@pytest.mark.parametrize("rung", LOCAL)
def test_redirect_to_hostname_resolving_private_fails_at_that_hop(env, rung):
    f = Fake(Raw(301, {"location": "https://inside.test/x"}, ""))
    with pytest.raises(FetchFailed) as e:
        env({rung: f}, resolve=resolver({"inside.test": ["172.16.0.9"]})).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "vetting" and len(f.calls) == 1


def test_unresolvable_host_is_vetting_failure_and_never_climbs(env):
    h, u = Fake(OK), Fake(OK)
    with pytest.raises(FetchFailed) as e:
        env({"httpx": h, "urllib": u}).fetch("https://nowhere.test/")
    assert e.value.kind == "vetting" and not h.calls and not u.calls


def test_browser_landing_on_private_address_is_vetting_failure(env):
    f = Fake(Raw(200, {}, "<p>x</p>", final_url="http://127.0.0.1/admin"))
    with pytest.raises(FetchFailed) as e:
        env({"playwright": f}).fetch("https://a.test/", rung="playwright")
    assert e.value.kind == "vetting"


# (v) 4 processes x N writes under the lock
N = 25


def _writer(store, home, worker):
    s, ledger = SiteStore(store), Ledger(home, DAY)
    for i in range(N):
        s.put(f"h{worker}-{i}.test", rung="httpx", won=DAY)
        assert ledger.take_hosted(f"hosted:r{worker % 2}", cap=10_000)


def test_concurrent_processes_lose_no_writes(tmp_path):
    store, home = init(tmp_path / ".fetch-ladder"), tmp_path / "home"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(store, home, w)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert all(p.exitcode == 0 for p in procs)
    hosts = json.loads((store / "sites.json").read_text())["hosts"]
    assert len(hosts) == 4 * N
    assert sum(Ledger(home, DAY).read()["hosted"].values()) == 4 * N


# (vi) reader 402 / 429 -> that reader stopped for the day, next reader tried, target host not blocked
@pytest.mark.parametrize("status", (402, 429))
def test_reader_refusal_stops_only_that_reader(env, status):
    fs = blocked_below({"hosted:jina": Fake(Raw(status, {}, "")), "hosted:firecrawl": Fake(hosted_ok())})
    s = env(fs)
    assert s.fetch("https://a.test/p").rung == "hosted:firecrawl"
    assert len(fs["hosted:jina"].calls) == 1  # no wait-out against a reader
    assert env.ledger.read()["stopped"] == ["hosted:jina"]
    # the target host is not blocked: a later httpx fetch on it is still sent
    fs["httpx"].answers = [OK]
    calls = len(fs["httpx"].calls)
    assert s.fetch("https://a.test/r", rung="httpx").status == 200 and len(fs["httpx"].calls) == calls + 1
    # a new session the same day skips the stopped reader without calling it
    env.store.put("b.test", rung="curl_cffi:chrome", won=DAY)
    fs2 = blocked_below({"hosted:jina": Fake(hosted_ok()), "hosted:firecrawl": Fake(hosted_ok())})
    assert env(fs2).fetch("https://b.test/").rung == "hosted:firecrawl" and not fs2["hosted:jina"].calls


# (vi-b) target-host 429 -> fails loud, no climb
def test_target_429_fails_loud_without_climbing(env):
    h, u = Fake(Raw(429, {"retry-after": "3600"}, "")), Fake(OK)
    with pytest.raises(FetchFailed) as e:
        env({"httpx": h, "urllib": u}).fetch("https://a.test/")
    assert e.value.kind == "rate_limited" and not u.calls and env.store.hosts() == {}


# (vi-c) saved rung meets 403 / challenge -> climbs and re-saves
@pytest.mark.parametrize("block", (Raw(403, {}, "forbidden"), Raw(401, {}, "who"), CHALLENGE))
def test_saved_rung_blocked_climbs_and_resaves(env, block):
    env.store.put("a.test", rung="urllib", won="2026-10-01", gap_s=4.0)
    fs = {"httpx": Fake(CHALLENGE), "urllib": Fake(block), "curl_cffi:chrome": Fake(OK)}
    page = env(fs).fetch("https://a.test/p")
    assert page.rung == "curl_cffi:chrome"
    assert env.store.get("a.test") == {"rung": "curl_cffi:chrome", "won": DAY, "gap_s": 4.0}
    assert env.ledger.read()["rung_changes"][0]["from"] == "urllib"


def test_saved_rung_transport_error_retried_twice_then_climbs(env):
    env.store.put("a.test", rung="httpx", won="2026-10-01")
    h = Fake(FetchTransportError("TLS refused"))
    fs = {"httpx": h, "urllib": Fake(OK)}
    assert env(fs).fetch("https://a.test/").rung == "urllib"
    assert len(h.calls) == 3  # ADR-008 D4: two retries on the same rung, then the climb


def test_transport_error_then_success_stays_on_rung(env):
    env.store.put("a.test", rung="httpx", won="2026-10-01")
    h, u = Fake(FetchTransportError("reset"), OK), Fake(OK)
    assert env({"httpx": h, "urllib": u}).fetch("https://a.test/").rung == "httpx" and not u.calls


def test_every_rung_failing_names_every_rung_tried(env, monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(FetchFailed) as e:
        env(blocked_below()).fetch("https://a.test/")
    assert e.value.kind == "exhausted"
    for rung in LOCAL:
        assert rung in str(e.value)
    assert env.store.hosts() == {}


# (vii) cap spent -> hosted-only fetch fails, saved rung unchanged
def test_cap_spent_fails_loud_and_keeps_saved_rung(env):
    env.store.put("a.test", rung="urllib", won="2026-10-01")
    for _ in range(30):
        assert env.ledger.take_hosted("hosted:jina", cap=30)
    fs = blocked_below({"hosted:jina": Fake(hosted_ok()), "hosted:firecrawl": Fake(hosted_ok())})
    with pytest.raises(FetchFailed) as e:
        env(fs).fetch("https://a.test/")
    assert e.value.kind == "cap" and not fs["hosted:jina"].calls and not fs["hosted:firecrawl"].calls
    assert env.store.get("a.test") == {"rung": "urllib", "won": "2026-10-01"}
    assert sum(env.ledger.read()["hosted"].values()) == 30


def test_cap_counts_explicit_hosted_calls(env):
    f = Fake(hosted_ok())
    s = env({"hosted:firecrawl": f}, cap=2)
    s.fetch("https://a.test/1", rung="hosted:firecrawl")
    s.fetch("https://a.test/2", rung="hosted:firecrawl")
    with pytest.raises(FetchFailed) as e:
        s.fetch("https://a.test/3", rung="hosted:firecrawl")
    assert e.value.kind == "cap" and len(f.calls) == 2


# (viii) one URL per D4 credential shape never reaches a hosted transport
CREDENTIAL = (
    "https://t.me/+AbCdEf", "https://telegram.me/joinchat/xyz", "https://chat.whatsapp.com/Ab12",
    "https://signal.group/#CjQKI", "https://discord.gg/abc", "https://discord.com/invite/abc",
    "https://docs.google.com/forms/d/e/1FAIpQL/viewform", "https://forms.gle/abc", "https://drive.google.com/file/d/1/view",
    "https://a.test/e?token=1", "https://a.test/e?KEY=1", "https://a.test/e?code=1", "https://a.test/e?Invite=1",
    "https://a.test/e?secret=1", "https://a.test/e?sig=1", "https://a.test/e?signature=1", "https://a.test/e?access=1",
    "https://a.test/e?x=1&auth=1",
)  # fmt: skip


@pytest.mark.parametrize("url", CREDENTIAL)
def test_credential_link_never_reaches_hosted(env, url):
    assert credential_shaped(url)
    hosts = {h: ["93.184.216.50"] for h in ("t.me", "telegram.me", "chat.whatsapp.com", "signal.group", "discord.gg",
                                            "discord.com", "docs.google.com", "forms.gle", "drive.google.com")}  # fmt: skip
    fs = blocked_below({r: Fake(hosted_ok()) for r in HOSTED})
    s = env(fs, resolve=resolver(hosts))
    with pytest.raises(FetchFailed):
        s.fetch(url)
    for rung in HOSTED:
        with pytest.raises(FetchFailed) as e:
            s.fetch(url, rung=rung)
        assert e.value.kind == "refused"
    assert all(not fs[r].calls for r in HOSTED)


@pytest.mark.parametrize(
    "url",
    ("https://a.test/e?id=1", "https://discord.com/channels/1", "https://docs.google.com/document/d/1",
     "https://a.test/e?access_token=1", "https://a.test/token/1", "https://me.test/"),
)  # fmt: skip
def test_non_credential_links_are_not_excluded(url):
    assert not credential_shaped(url)


# store format and resolution
def test_newer_store_version_fails_loud(env, tmp_path):
    (env.store.path).write_text(json.dumps({"version": 99, "hosts": {}}))
    with pytest.raises(StoreError, match="newer"):
        env({"httpx": Fake(OK)}).fetch("https://a.test/")


def test_explicit_rung_never_writes_the_store(env):
    env({"urllib": Fake(OK)}).fetch("https://a.test/", rung="urllib")
    assert env.store.hosts() == {} and env.ledger.read()["rung_changes"] == []


def test_climb_writes_the_store_even_for_a_known_host(env):
    env.store.put("a.test", rung="curl_cffi:chrome", won="2026-10-01")
    page = env({"httpx": Fake(OK)}).climb("https://a.test/")
    assert page.rung == "httpx" and env.store.get("a.test")["rung"] == "httpx"


def test_saved_gap_paces_the_host_across_sessions(env):
    env.store.put("a.test", rung="httpx", won=DAY, gap_s=5.0)
    s1, s2 = env({"httpx": Fake(OK)}), env({"httpx": Fake(OK)})
    s2._clock, s2._sleep = s1.test_clock, s1.test_clock.sleep  # one wall clock, two processes' sessions
    s1.fetch("https://a.test/1")
    s2.fetch("https://a.test/2")
    assert s1.test_clock.slept == [5.0]


def test_project_store_read_first_then_global_and_writes_go_to_project(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    monkeypatch.setenv("FETCH_LADDER_HOME", str(home))
    monkeypatch.chdir(repo)
    SiteStore(home).put("a.test", rung="urllib", won="2026-10-01")
    clock = Clock()
    mk = lambda fs: Session(transports=fs, sleep=clock.sleep, clock=clock, resolve=resolver())
    assert mk({"urllib": Fake(OK)}).fetch("https://a.test/").rung == "urllib"  # global, no project store
    mk({"httpx": Fake(OK)}).fetch("https://b.test/")
    assert SiteStore(home).get("b.test")["rung"] == "httpx"  # not opted in: writes go global
    init(repo / ".fetch-ladder")
    SiteStore(repo / ".fetch-ladder").put("a.test", rung="curl_cffi:chrome", won="2026-10-02")
    fs = {"curl_cffi:chrome": Fake(OK), "urllib": Fake(OK)}
    assert mk(fs).fetch("https://a.test/").rung == "curl_cffi:chrome"  # project first
    fs = {"httpx": Fake(OK)}
    mk(fs).fetch("https://b.test/x", rung=None)  # known in global: used, nothing written
    Session(transports={"httpx": Fake(OK)}, sleep=clock.sleep, clock=clock,
            resolve=resolver({"c.test": ["93.184.216.36"]})).fetch("https://c.test/")  # fmt: skip
    assert SiteStore(repo / ".fetch-ladder").get("c.test")["rung"] == "httpx"
    assert SiteStore(home).get("c.test") is None
    assert (Ledger(home, DAY).path).exists() and not (repo / ".fetch-ladder" / "ledger").exists()
