"""The CLI's store verbs and write rules, on fakes (the Session's transports are swapped in)."""

import json
import subprocess

import pytest

from fetch_ladder import Raw, SiteStore, cli
from fetch_ladder.store import init

OK = Raw(200, {}, "<p>hello</p>")


@pytest.fixture
def fake_session(monkeypatch, tmp_path):
    monkeypatch.setenv("FETCH_LADDER_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    real = cli.Session

    def make(**kw):
        return real(transports={"httpx": lambda m, u, f: OK}, resolve=lambda h: ["93.184.216.34"], sleep=lambda s: None, **kw)

    monkeypatch.setattr(cli, "Session", make)
    return tmp_path


def test_init_creates_store_at_git_toplevel(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["init"]) == 0
    assert json.loads((repo / ".fetch-ladder" / "sites.json").read_text()) == {"version": 1, "hosts": {}}
    assert "*.lock" in (repo / ".fetch-ladder" / ".gitignore").read_text()


def test_init_outside_git_needs_a_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 1
    assert cli.main(["init", str(tmp_path / "box")]) == 0
    assert (tmp_path / "box" / ".fetch-ladder" / "sites.json").exists()


def test_by_site_fetch_saves_and_list_shows_it(fake_session, capsys):
    store = init(fake_session / "s")
    assert cli.main(["https://a.test/", "--store", str(store)]) == 0
    assert SiteStore(store).get("a.test")["rung"] == "httpx"
    capsys.readouterr()
    assert cli.main(["list", "--store", str(store), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [{"store": str(store), "host": "a.test", "rung": "httpx", "won": out[0]["won"]}]


def test_explicit_rung_never_writes_and_climb_does(fake_session):
    store = init(fake_session / "s")
    assert cli.main(["https://a.test/", "--rung", "httpx", "--store", str(store)]) == 0
    assert SiteStore(store).hosts() == {}
    assert cli.main(["https://a.test/", "--climb", "--store", str(store)]) == 0
    assert SiteStore(store).get("a.test")["rung"] == "httpx"


def test_rung_and_climb_are_exclusive(fake_session):
    with pytest.raises(SystemExit):
        cli.main(["https://a.test/", "--rung", "httpx", "--climb"])
