"""Fetch-ladder behaviour on fake transports: no live network, no hosted reader is ever called."""

import pytest

from fetch_ladder import FetchFailed, FetchTransportError, Raw, RungUnavailable, Session

OK = Raw(200, {}, "<html><body><p>hello</p><a href='/x'>x</a><script>var a=1</script></body></html>")


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


def session(transports, **kw):
    clock = Clock()
    s = Session(transports=transports, sleep=clock.sleep, clock=clock, **kw)
    s.test_clock = clock
    return s


BUILT = ("httpx", "urllib", "curl_cffi:chrome")


def test_default_rung_is_httpx_and_page_shape():
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
        session({"httpx": f}).fetch("https://a.test/")
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
def test_hosted_refused_without_flag_and_no_request(rung):
    f = Fake(OK)
    with pytest.raises(FetchFailed) as e:
        session({rung: f}).fetch("https://a.test/", rung=rung)
    assert e.value.kind == "refused" and not f.calls


@pytest.mark.parametrize("rung", ("hosted:jina", "hosted:firecrawl"))
def test_hosted_runs_with_flag_and_vets_target_only(rung):
    f = Fake(OK)
    s = session({rung: f}, allow_hosted=True)
    assert s.fetch("https://a.test/", rung=rung).rung == rung
    with pytest.raises(FetchFailed) as e:
        s.fetch("https://r.jina.ai/https://a.test/", rung=rung)
    assert e.value.kind == "vetting" and len(f.calls) == 1


@pytest.mark.parametrize("rung,var", (("hosted:jina", "JINA_API_KEY"), ("hosted:firecrawl", "FIRECRAWL_API_KEY")))
def test_hosted_without_key_is_unavailable(rung, var, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(RungUnavailable):
        session({}, allow_hosted=True).fetch("https://a.test/", rung=rung)


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
    assert page.rung == "curl_cffi:chrome" and len(h.calls) == len(u.calls) == len(c.calls) == 1
    assert not any("firefox" in n and "request" in n for n in notes)


def test_climb_skips_unbuilt_playwright_and_ends_before_hosted_without_flag():
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    hosted = Fake(OK)
    fs["hosted:jina"] = hosted
    notes = []
    with pytest.raises(FetchFailed):
        session(fs).climb("https://a.test/", note=notes.append)
    assert not hosted.calls
    assert any("playwright" in n and "not built" in n for n in notes)


def test_climb_reaches_hosted_with_flag_and_skips_keyless(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    fs["hosted:jina"] = Fake(OK)
    page = session(fs, allow_hosted=True).climb("https://a.test/")
    assert page.rung == "hosted:jina"


def test_climb_keyless_hosted_skipped_noted(monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    fs = {r: Fake(Raw(403, {}, "no")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    notes = []
    with pytest.raises(FetchFailed):
        session(fs, allow_hosted=True).climb("https://a.test/", note=notes.append)
    assert any("hosted:jina" in n and "unavailable" in n for n in notes)


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


def test_climb_all_transport_errors_raises_transport_error():
    fs = {r: Fake(FetchTransportError("down")) for r in ("httpx", "urllib", "curl_cffi:chrome", "curl_cffi:firefox", "curl_cffi:safari")}
    with pytest.raises(FetchTransportError):
        session(fs).climb("https://a.test/")
