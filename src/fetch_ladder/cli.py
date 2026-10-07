import argparse
import json
import sys

from fetch_ladder import FetchFailed, FetchTransportError, Session
from fetch_ladder.ladder import GAP_S


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fetch-ladder", description="Fetch one URL up a ladder of clients, politely.")
    ap.add_argument("url")
    ap.add_argument("--rung", default="httpx", help="httpx | urllib | curl_cffi:<target> | playwright | hosted:jina | hosted:firecrawl (default httpx)")
    ap.add_argument("--climb", action="store_true", help="try each rung once, in order, until a usable body")
    ap.add_argument("--allow-hosted", action="store_true", help="let hosted readers run (keys: JINA_API_KEY, FIRECRAWL_API_KEY)")
    ap.add_argument("--gap", type=float, default=GAP_S, help="minimum seconds between requests to one host")
    ap.add_argument("--form", help="URL-encoded body to POST instead of GET")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print body, text, final_url, status, links, rung as JSON")
    out.add_argument("--text", action="store_true", help="print readable text instead of the raw body")
    a = ap.parse_args(argv)
    s = Session(allow_hosted=a.allow_hosted, gap_s=a.gap)
    try:
        page = s.climb(a.url, a.form, lambda m: print(m, file=sys.stderr)) if a.climb else s.fetch(a.url, a.rung, a.form)
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
