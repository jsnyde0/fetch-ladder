import argparse
import json
import sys
from pathlib import Path

from fetch_ladder import FetchFailed, FetchTransportError, Session, StoreError
from fetch_ladder import store as stores
from fetch_ladder.ladder import GAP_S

USAGE = """fetch-ladder URL [options]   fetch one URL (by site unless --rung)
fetch-ladder init [DIR]       opt DIR (default: the git toplevel) in to a project store
fetch-ladder list [--store S] [--json]   list the site memory"""


def _init(argv) -> int:
    ap = argparse.ArgumentParser(prog="fetch-ladder init", description="Create a project store folder, .fetch-ladder/.")
    ap.add_argument("dir", nargs="?", help="where to create .fetch-ladder/ (default: the git toplevel)")
    a = ap.parse_args(argv)
    parent = Path(a.dir) if a.dir else stores.git_toplevel()
    if parent is None:
        print("fetch-ladder init: not inside a git repo; name the folder: fetch-ladder init DIR", file=sys.stderr)
        return 1
    print(stores.init(parent / stores.DIRNAME))
    return 0


def _list(argv) -> int:
    ap = argparse.ArgumentParser(prog="fetch-ladder list", description="List the site memory: project store first, then global.")
    ap.add_argument("--store", type=Path, help="one store folder only")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.store:
        folders = [a.store]
    else:
        project = stores.project_store()
        folders = ([project] if project else []) + [stores.home()]
    rows = [
        {"store": str(folder), "host": host, **entry}
        for folder in folders
        for host, entry in sorted(stores.SiteStore(folder).hosts().items())
    ]
    if a.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            gap = f"  gap {r['gap_s']}s" if "gap_s" in r else ""
            print(f"{r['host']}  {r['rung']}  won {r.get('won', '?')}{gap}  ({r['store']})")
    return 0


def _fetch(argv) -> int:
    ap = argparse.ArgumentParser(prog="fetch-ladder", description="Fetch one URL by site, politely.", epilog=USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    ap.add_argument("url")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--rung", help="only this rung, never writing the store: httpx | urllib | curl_cffi:<target> | playwright | hosted:jina | hosted:firecrawl")
    how.add_argument("--climb", action="store_true", help="climb every rung in order even for a known host, and save the winner")
    ap.add_argument("--no-hosted", action="store_true", help="local rungs only (hosted readers need FIRECRAWL_API_KEY; capped per day)")
    ap.add_argument("--store", type=Path, help="read and write this store folder only")
    ap.add_argument("--gap", type=float, default=GAP_S, help="minimum seconds between requests to one host")
    ap.add_argument("--form", help="URL-encoded body to POST instead of GET")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print body, text, final_url, status, links, rung as JSON")
    out.add_argument("--text", action="store_true", help="print readable text instead of the raw body")
    a = ap.parse_args(argv)
    s = Session(allow_hosted=not a.no_hosted, gap_s=a.gap, store=a.store)
    note = lambda m: print(m, file=sys.stderr)
    try:
        page = s.climb(a.url, a.form, note) if a.climb else s.fetch(a.url, a.rung, a.form, note)
    except (FetchFailed, FetchTransportError) as e:
        print(f"fetch-ladder: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"body": page.body, "text": page.text, "final_url": page.final_url, "status": page.status,
                          "links": list(page.links), "rung": page.rung}))  # fmt: skip
    else:
        print(page.text if a.text else page.body)
        print(f"rung: {page.rung}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    verb = {"init": _init, "list": _list}.get(argv[0] if argv else "")
    try:
        return verb(argv[1:]) if verb else _fetch(argv)
    except StoreError as e:
        print(f"fetch-ladder: {e}", file=sys.stderr)
        return 1
