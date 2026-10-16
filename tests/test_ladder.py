"""Fetch-ladder behaviour on fake transports: no live network, no hosted reader is ever called."""

import pytest

from fetch_ladder import HOSTED_CAP, FetchFailed, FetchTransportError, Raw, RungUnavailable, Session

OK = Raw(200, {}, "<html><body><p>hello</p><a href='/x'>x</a><script>var a=1</script></body></html>")
HOSTED_OK = Raw(200, {}, "<p>via reader</p>", target_status=200)


class Fake:
    """A transport that answers from a script (a Raw or an exception per call) and logs calls."""

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


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FETCH_LADDER_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)  # no git toplevel: the global store only


def public(host):
    return ["93.184.216.34"]


def session(transports, **kw):
    clock = Clock()
    s = Session(transports=transports, sleep=clock.sleep, clock=clock, resolve=public, **kw)
    s.test_clock = clock
    return s


BUILT = ("httpx", "urllib", "curl_cffi:chrome")


def test_unknown_host_tries_httpx_first_and_page_shape():
    f = Fake(OK)
    page = session({"httpx": f}).fetch("https://a.test/p")
    assert page.rung == "httpx" and page.status == 200
    assert page.body == OK.text
    assert "hello" in page.text and "var a" not in page.text
    assert page.links == ("https://a.test/x",)
    assert page.final_url == "https://a.test/p"


def test_rung_selection_uses_named_transport_only():
    h, u = Fake(OK), Fake(OK)
    page = session({"httpx": h, "urllib": u}).fetch("https://a.test/", rung="urllib")
    assert page.rung == "urllib" and not h.calls and len(u.calls) == 1


def test_post_form_passed_through():
    f = Fake(OK)
    session({"httpx": f}).fetch("https://a.test/ajax", form="a=1")
    assert f.calls == [("POST", "https://a.test/ajax", "a=1")]


def test_unknown_rung_fails_loud():
    with pytest.raises(FetchFailed) as e:
        session({}).fetch("https://a.test/", rung="bogus")
    assert e.value.kind == "unavailable"


@pytest.mark.parametrize("rung", BUILT)
def test_404_is_fetchfailed_with_status_per_rung(rung):
    f = Fake(Raw(404, {}, "nope"))
    with pytest.raises(FetchFailed) as e:
        session({rung: f}).fetch("https://a.test/", rung=rung)
    assert e.value.status == 404 and e.value.kind == "status"
    assert len(f.calls) == 1  # status errors never retry


@pytest.mark.parametrize("rung", BUILT)
def test_challenge_is_failure_never_text(rung):
    f = Fake(Raw(200, {}, "<title>Just a moment...</title>"))
    with pytest.raises(FetchFailed) as e:
        session({rung: f}).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "challenge"


def test_challenge_on_403():
    f = Fake(Raw(403, {}, "Attention Required! | Cloudflare"))
    with pytest.raises(FetchFailed) as e:
        session({"httpx": f}).fetch("https://a.test/", rung="httpx")
    assert e.value.kind == "challenge" and e.value.status == 403


@pytest.mark.parametrize("rung", BUILT)
def test_transport_error_passes_as_its_own_kind_without_retry(rung):
    f = Fake(FetchTransportError("timeout"))
    with pytest.raises(FetchTransportError):
        session({rung: f}).fetch("https://a.test/", rung=rung)
    assert len(f.calls) == 1


@pytest.mark.parametrize("rung", BUILT)
def test_pacing_one_second_per_host_per_rung(rung):
    s = session({rung: Fake(OK)})
    s.fetch("https://a.test/1", rung=rung)
    s.fetch("https://a.test/2", rung=rung)
    s.fetch("https://b.test/1", rung=rung)  # other host: no wait
    assert s.test_clock.slept == [1.0]


def test_gap_configurable_and_shared_across_rungs():
    s = session({"httpx": Fake(OK), "urllib": Fake(OK)}, gap_s=2.0)
    s.fetch("https://a.test/1", rung="httpx")
    s.fetch("https://a.test/2", rung="urllib")
    assert s.test_clock.slept == [2.0]


@pytest.mark.parametrize("rung", BUILT)
def test_429_retry_after_waited_then_ok(rung):
    f = Fake(Raw(429, {"retry-after": "7"}, ""), OK)
    s = session({rung: f})
    assert s.fetch("https://a.test/", rung=rung).status == 200
    assert 7.0 in s.test_clock.slept and len(f.calls) == 2


def test_429_without_retry_after_waits_30():
    s = session({"httpx": Fake(Raw(429, {}, ""), OK)})
    s.fetch("https://a.test/")
    assert 30.0 in s.test_clock.slept


def test_429_exhausted_after_two_retries_blocks_host_across_rungs():
    h, u = Fake(Raw(429, {}, "")), Fake(OK)
    s = session({"httpx": h, "urllib": u})
    with pytest.raises(FetchFailed) as e:
        s.fetch("https://a.test/")
    assert e.value.kind == "rate_limited" and e.value.status == 429
    assert len(h.calls) == 3  # first try plus two retries
    with pytest.raises(FetchFailed) as e:
        s.fetch("https://a.test/other", rung="urllib")
    assert e.value.kind == "rate_limited" and not u.calls
    assert s.fetch("https://b.test/", rung="urllib").status == 200  # other host fine


def test_429_with_huge_retry_after_blocks_at_once():
    f = Fake(Raw(429, {"retry-after": "3600"}, ""))
    s = session({"httpx": f})
    with pytest.raises(FetchFailed):
        s.fetch("https://a.test/")
    assert len(f.calls) == 1


# vetting
@pytest.mark.parametrize(
    "url",
    [
        "https://r.jina.ai/https://a.test/",
        "https://api.firecrawl.dev/x",
        "https://pure.md/x",
        "https://12ft.io/x",
        "https://archive.ph/x",
        "https://web.archive.org/web/1/x",
        "https://a-test.translate.goog/x",
        "https://a.test/proxy/https://b.test/",
        "https://a.test/p?u=https%3A%2F%2Fb.test",
        "ftp://a.test/x",
    ],
)
@pytest.mark.parametrize("rung", BUILT)
def test_target_vetting_per_rung(url, rung):
    f = Fake(OK)
    with pytest.raises(FetchFailed) as e:
        session({rung: f}).fetch(url, rung=rung)
    assert e.value.kind == "vetting" and not f.calls


@pytest.mark.parametrize("rung", BUILT)
def test_redirect_into_reader_fails_at_that_hop(rung):
    f = Fake(Raw(302, {"location": "https://r.jina.ai/https://a.test/"}, ""))
    with pytest.raises(FetchFailed) as e:
        session({rung: f}).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "vetting" and "r.jina.ai" in str(e.value)
    assert len(f.calls) == 1  # the reader was never requested


@pytest.mark.parametrize("rung", BUILT)
def test_relative_redirect_followed_and_final_url_reported(rung):
    f = Fake(Raw(301, {"location": "/new"}, ""), OK)
    page = session({rung: f}).fetch("https://a.test/old", rung=rung)
    assert page.final_url == "https://a.test/new" and f.calls[1][1] == "https://a.test/new"


def test_redirect_loop_fails():
    f = Fake(Raw(302, {"location": "/again"}, ""))
    with pytest.raises(FetchFailed) as e:
        session({"httpx": f}).fetch("https://a.test/")
    assert e.value.kind == "redirects"


def test_browser_final_url_is_vetted():
    f = Fake(Raw(200, {}, "<p>x</p>", final_url="https://archive.ph/abc"))
    with pytest.raises(FetchFailed) as e:
        session({"playwright": f}).fetch("https://a.test/", rung="playwright")
    assert e.value.kind == "vetting"


# hosted
@pytest.mark.parametrize("rung", ("hosted:jina", "hosted:firecrawl"))
def test_hosted_refused_when_off_and_no_request(rung):
    f = Fake(HOSTED_OK)
    with pytest.raises(FetchFailed) as e:
        session({rung: f}, allow_hosted=False).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "refused" and not f.calls


@pytest.mark.parametrize("rung", ("hosted:jina", "hosted:firecrawl"))
def test_hosted_runs_by_default_and_vets_target_only(rung):
    f = Fake(HOSTED_OK)
    s = session({rung: f})
    assert s.fetch("https://a.test/", rung=rung).rung == rung
    with pytest.raises(FetchFailed) as e:
        s.fetch("https://r.jina.ai/https://a.test/", rung=rung)
    assert e.value.kind == "vetting" and len(f.calls) == 1


def test_firecrawl_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(RungUnavailable):
        session({}).fetch("https://a.test/", rung="hosted:firecrawl")


def test_playwright_not_built():
    with pytest.raises(RungUnavailable, match="rung not built"):
        session({}).fetch("https://a.test/", rung="playwright")


# climb
def test_climb_stops_at_first_good_body_and_skips_unbuilt(monkeypatch):
    h = Fake(Raw(403, {}, "denied"))
    u = Fake(FetchTransportError("reset"))
    c = Fake(OK)
    notes = []
    s = session({"httpx": h, "urllib": u, "curl_cffi:chrome": c})
    page = s.climb("https://a.test/", note=notes.append)
    assert page.rung == "curl_cffi:chrome" and len(h.calls) == len(c.calls) == 1
    assert len(u.calls) == 3  # a transport error is retried twice on its rung before the climb moves on
    assert not any("firefox" in n and "request" in n for n in notes)


def test_climb_skips_unbuilt_playwright_and_ends_before_hosted_when_off():
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    hosted = Fake(OK)
    fs["hosted:jina"] = hosted
    notes = []
    with pytest.raises(FetchFailed):
        session(fs, allow_hosted=False).climb("https://a.test/", note=notes.append)
    assert not hosted.calls
    assert any("playwright" in n and "not built" in n for n in notes)


def test_climb_reaches_hosted_and_skips_keyless(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    fs["hosted:jina"] = Fake(HOSTED_OK)
    page = session(fs).climb("https://a.test/")
    assert page.rung == "hosted:jina"


def test_climb_keyless_hosted_skipped_noted(monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    fs["hosted:jina"] = Fake(Raw(402, {}, ""))  # keyless jina is live; only firecrawl needs a key
    notes = []
    with pytest.raises(FetchFailed):
        session(fs).climb("https://a.test/", note=notes.append)
    assert any("hosted:firecrawl" in n and "unavailable" in n for n in notes)


def test_climb_stops_on_429_exhaustion():
    h, u = Fake(Raw(429, {}, "")), Fake(OK)
    with pytest.raises(FetchFailed) as e:
        session({"httpx": h, "urllib": u}).climb("https://a.test/")
    assert e.value.kind == "rate_limited" and not u.calls


def test_climb_stops_on_vetting_failure():
    h = Fake(Raw(302, {"location": "https://r.jina.ai/x"}, ""))
    u = Fake(OK)
    with pytest.raises(FetchFailed) as e:
        session({"httpx": h, "urllib": u}).climb("https://a.test/")
    assert e.value.kind == "vetting" and not u.calls


def test_climb_all_transport_errors_is_exhausted_naming_each_rung():
    fs = {r: Fake(FetchTransportError("down")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    with pytest.raises(FetchFailed) as e:
        session(fs, allow_hosted=False).climb("https://a.test/")
    assert e.value.kind == "exhausted" and "curl_cffi:safari: down" in str(e.value)


# the firecrawl transport's answer mapping (its client answers from a MockTransport; no network)
def _firecrawl(monkeypatch, handler):
    import httpx

    from fetch_ladder import transports

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    return transports.firecrawl_transport()


def test_firecrawl_reports_target_status_and_asks_for_no_cache(monkeypatch):
    import json

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return __import__("httpx").Response(200, json={"data": {"html": "<p>x</p>", "metadata": {"statusCode": 404}}})

    raw = _firecrawl(monkeypatch, handler)("GET", "https://a.test/", None)
    assert raw.status == 200 and raw.target_status == 404
    assert sent[0]["maxAge"] == 0 and sent[0]["url"] == "https://a.test/"


def test_firecrawl_without_metadata_hides_target_status(monkeypatch):
    raw = _firecrawl(monkeypatch, lambda r: __import__("httpx").Response(200, json={"data": {"html": "<p>x</p>"}}))(
        "GET", "https://a.test/", None
    )
    assert raw.target_status is None


def _jina(monkeypatch, handler, key="k"):
    import httpx

    from fetch_ladder import transports

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    if key is None:
        monkeypatch.delenv("JINA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("JINA_API_KEY", key)
    return transports.jina_transport()


def test_jina_reports_target_status_and_asks_for_json_and_no_cache(monkeypatch):
    import httpx

    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"content": "# x", "httpStatus": 200}})

    raw = _jina(monkeypatch, handler)("GET", "https://a.test/p", None)
    assert raw.status == 200 and raw.target_status == 200 and raw.text == "# x"
    h = sent[0].headers
    assert str(sent[0].url) == "https://r.jina.ai/https://a.test/p"
    assert h["accept"] == "application/json" and h["x-no-cache"] == "true" and h["authorization"] == "Bearer k"


def test_jina_without_key_is_live_and_sends_no_authorization(monkeypatch):
    import httpx

    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"data": {"content": "x", "httpStatus": 200}})

    send = _jina(monkeypatch, handler, key=None)
    assert send("GET", "https://a.test/", None).target_status == 200
    assert "authorization" not in sent[0].headers


def test_jina_reports_a_404_target(monkeypatch):
    import httpx

    raw = _jina(monkeypatch, lambda r: httpx.Response(200, json={"data": {"content": "gone", "httpStatus": 404}}))(
        "GET", "https://a.test/", None
    )
    assert raw.status == 200 and raw.target_status == 404


def test_jina_without_http_status_hides_target_status(monkeypatch):
    import httpx

    raw = _jina(monkeypatch, lambda r: httpx.Response(200, json={"data": {"content": "x"}}))("GET", "https://a.test/", None)
    assert raw.target_status is None


def test_jina_404_target_fails_loud_without_climbing(monkeypatch):
    import httpx

    fs = {"hosted:jina": _jina(monkeypatch, lambda r: httpx.Response(200, json={"data": {"content": "gone", "httpStatus": 404}}))}
    fs["hosted:firecrawl"] = Fake(OK)
    with pytest.raises(FetchFailed):
        session(fs).fetch("https://a.test/", rung="hosted:jina")
    assert not fs["hosted:firecrawl"].calls


# a dead network (ADR-008 D4): retried, then loud; never a climb to hosted readers or a spent cap slot
LOCAL = ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")


def test_climb_dead_network_fails_loud_without_touching_hosted_rungs():
    fs = {r: Fake(FetchTransportError("dns down")) for r in LOCAL}
    fs.update({r: Fake(Raw(200, {}, "<p>x</p>", target_status=200)) for r in ("hosted:jina", "hosted:firecrawl")})
    s = session(fs)
    with pytest.raises(FetchFailed) as e:
        s.climb("https://a.test/")
    assert e.value.kind == "network" and "network" in str(e.value) and "dns down" in str(e.value)
    assert all(len(fs[r].calls) == 3 for r in LOCAL)  # first try + 2 retries
    assert not fs["hosted:jina"].calls and not fs["hosted:firecrawl"].calls
    assert s.ledger.read()["hosted"] == {}


def test_climb_one_blocked_local_rung_still_climbs_to_hosted():
    fs = {r: Fake(FetchTransportError("down")) for r in LOCAL}
    fs["httpx"] = Fake(Raw(403, {}, "no"))
    fs["hosted:jina"] = Fake(Raw(200, {}, "<p>x</p>", target_status=200))
    assert session(fs).climb("https://a.test/").rung == "hosted:jina"


def test_hosted_request_that_never_sent_takes_no_cap_slot():
    fs = {"hosted:jina": Fake(FetchTransportError("down"))}
    s = session(fs)
    with pytest.raises(FetchTransportError):
        s.fetch("https://a.test/", rung="hosted:jina")
    assert s.ledger.read()["hosted"] == {}


def test_hosted_request_that_was_sent_takes_a_cap_slot():
    fs = {"hosted:jina": Fake(Raw(200, {}, "<p>x</p>", target_status=200))}
    s = session(fs)
    s.fetch("https://a.test/", rung="hosted:jina")
    assert s.ledger.read()["hosted"] == {"hosted:jina": 1}


def test_budget_reserve_stops_adhoc_while_nightly_keeps_slots():
    ok = Raw(200, {}, "<p>x</p>", target_status=200)
    adhoc = session({"hosted:jina": Fake(ok)}, cap=5, nightly_reserve=2)
    nightly = session({"hosted:jina": Fake(ok)}, cap=5, nightly_reserve=2, caller="nightly")
    for _ in range(3):
        adhoc.fetch("https://a.test/", rung="hosted:jina")
    with pytest.raises(FetchFailed) as e:
        adhoc.fetch("https://a.test/", rung="hosted:jina")
    assert e.value.kind == "cap"
    for _ in range(2):  # the nightly takes the reserved slots on the same day ledger
        nightly.fetch("https://a.test/", rung="hosted:jina")
    with pytest.raises(FetchFailed) as e:
        nightly.fetch("https://a.test/", rung="hosted:jina")
    assert e.value.kind == "cap"
    assert nightly.ledger.read()["hosted"] == {"hosted:jina": 5}


def test_budget_default_reserve_is_20_of_30_and_unknown_caller_is_loud():
    from fetch_ladder import NIGHTLY_RESERVE

    assert (HOSTED_CAP, NIGHTLY_RESERVE) == (30, 20)
    with pytest.raises(ValueError):
        Session(caller="cron")
