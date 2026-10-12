"""The site memory and the day ledger: JSON files, every read-modify-write under a file lock,
every write an atomic replace.

Site memory (`sites.json` in a store folder): per host, the working rung, the date it was won,
and an optional pacing gap. The global store lives in the home folder (FETCH_LADDER_HOME, else
~/.fetch-ladder), unversioned. A repo opts in by having a `.fetch-ladder/` folder at its git
toplevel (`fetch-ladder init`); lookup reads the project store first, then global, and writes go
to the project store.

Day ledger (`ledger/<UTC day>.json` in the home folder, whatever store a call resolves; never
committed): hosted calls per reader, readers stopped for the day, rung changes, and per-host
next-request times, so pacing and the hosted cap hold across processes.
"""

import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

VERSION = 1
HOME_ENV = "FETCH_LADDER_HOME"
DIRNAME = ".fetch-ladder"
SITES = "sites.json"
GITIGNORE = "*.lock\n*.tmp\n"


class StoreError(Exception):
    """A store or ledger file is unreadable, or written by a newer fetch-ladder."""


def home() -> Path:
    value = os.environ.get(HOME_ENV, "").strip()
    return Path(value).expanduser() if value else Path.home() / DIRNAME


def utc_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).date().isoformat()


def git_toplevel(cwd: Path | None = None) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def project_store(cwd: Path | None = None) -> Path | None:
    """The opted-in project store folder at the git toplevel, if there is one."""
    top = git_toplevel(cwd)
    return top / DIRNAME if top is not None and (top / DIRNAME).is_dir() else None


def init(folder: Path) -> Path:
    """Create a store folder (an empty site memory and a .gitignore for the lock files)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".gitignore").write_text(GITIGNORE)
    sites = folder / SITES
    if not sites.exists():
        _write(sites, {"version": VERSION, "hosts": {}})
    return folder


def _read(path: Path, empty: Callable[[], dict]) -> dict:
    if not path.exists():
        return empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise StoreError(f"{path}: unreadable ({e})") from e
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, int):
        raise StoreError(f"{path}: no format version")
    if version > VERSION:
        raise StoreError(f"{path}: format version {version} is newer than this fetch-ladder reads ({VERSION})")
    return data


def _write(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _update(path: Path, empty: Callable[[], dict], change: Callable[[dict], object]):
    with _locked(path):
        data = _read(path, empty)
        result = change(data)
        _write(path, data)
        return result


class SiteStore:
    """One store folder's site memory."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / SITES

    @staticmethod
    def _empty() -> dict:
        return {"version": VERSION, "hosts": {}}

    def hosts(self) -> dict[str, dict]:
        return _read(self.path, self._empty)["hosts"]

    def get(self, host: str) -> dict | None:
        return self.hosts().get(host)

    def put(self, host: str, **fields) -> None:
        """Merge `fields` (rung, won, gap_s) into the host's entry."""

        def change(data):
            data["hosts"].setdefault(host, {}).update(fields)

        _update(self.path, self._empty, change)


class Ledger:
    """One machine's ledger for one UTC day."""

    def __init__(self, folder: Path, day: str):
        self.day = day
        self.path = Path(folder) / "ledger" / f"{day}.json"

    def _empty(self) -> dict:
        return {"version": VERSION, "day": self.day, "hosted": {}, "stopped": [], "rung_changes": [], "next_request": {}}

    def read(self) -> dict:
        return _read(self.path, self._empty)

    def take_hosted(self, reader: str, cap: int) -> bool:
        """Count one hosted call for `reader` if today's total is under `cap`."""

        def change(data):
            if sum(data["hosted"].values()) >= cap:
                return False
            data["hosted"][reader] = data["hosted"].get(reader, 0) + 1
            return True

        return _update(self.path, self._empty, change)

    def stop(self, reader: str) -> None:
        def change(data):
            if reader not in data["stopped"]:
                data["stopped"].append(reader)

        _update(self.path, self._empty, change)

    def stopped(self, reader: str) -> bool:
        return reader in self.read()["stopped"]

    def rung_change(self, host: str, old: str | None, new: str, store: Path) -> None:
        def change(data):
            data["rung_changes"].append({"host": host, "from": old, "to": new, "store": str(store)})

        _update(self.path, self._empty, change)

    def reserve(self, host: str, gap_s: float, now: float) -> float:
        """Book the host's next request slot (no earlier than `now`, `gap_s` after the last one)
        and return it; the caller waits until then."""

        def change(data):
            slot = max(now, data["next_request"].get(host, float("-inf")))
            data["next_request"][host] = slot + gap_s
            return slot

        return _update(self.path, self._empty, change)
