# fetch-ladder

Fetch a URL **by site**: the package remembers, per host, which client (rung) works, climbs to find one for an unknown or newly blocked host, and keeps shared manners — per-host pacing across processes, HTTP 429 wait-out, challenge pages as failures, vetting of every hop, a daily hosted-reader cap.

Rungs, in climb order: `httpx`, `urllib`, `curl_cffi:chrome`, `curl_cffi:firefox`, `curl_cffi:safari`, `playwright` (not built), `hosted:jina` (unavailable: its answers hide the target's status), `hosted:firecrawl` (key `FIRECRAWL_API_KEY`).

    from fetch_ladder import Session
    page = Session().fetch(url)                  # by site; Page(body, text, final_url, status, links, rung)
    page = Session().fetch(url, rung="urllib")   # one rung only; never writes the store
    page = Session().climb(url)                  # climb even for a known host; saves the winner

    uvx --from ~/code/personal/fetch-ladder fetch-ladder URL [--rung R | --climb] [--no-hosted] [--store DIR] [--gap S] [--form BODY] [--json | --text]
    fetch-ladder init [DIR]                      # opt a repo in to a project store
    fetch-ladder list [--store DIR] [--json]     # show the site memory

Failures raise `FetchFailed` (its `kind` says which) or, on an explicit rung, `FetchTransportError`.

## By site: when it climbs, when it fails loud

- Unknown host: climb. Saved rung: use it; if it meets a **block** — a bot-check page, 401/403, or a transport error (connect/TLS/timeout) that survives two retries on that rung — climb from the bottom, skipping it.
- A winner is a 2xx that is not a challenge page. It is saved (rung + the UTC day it won), and the change goes in the day ledger.
- Everything else fails loud **without climbing**: other 4xx/5xx (a dead link), vetting failures, the target's 429, a spent hosted cap.
- Every rung failing: `FetchFailed(kind="exhausted")` naming each rung and its failure.

## Stores

- **Global store:** `~/.fetch-ladder/sites.json` (or `$FETCH_LADDER_HOME/sites.json`), unversioned.
- **Project store:** `<git toplevel>/.fetch-ladder/sites.json`, created by `fetch-ladder init`; its presence opts the repo in. Lookup reads the project store first, then global; writes go to the project store. Commit `sites.json`; the folder's `.gitignore` keeps lock and temp files out.
- **Explicit store:** `Session(store=DIR)` / `--store DIR` reads and writes that folder alone (for places with no git toplevel, e.g. a container).
- **Day ledger:** `~/.fetch-ladder/ledger/<UTC day>.json` (or under `$FETCH_LADDER_HOME`), always in the home folder whatever store a call uses, never committed. It holds hosted calls per reader, readers stopped for the day, rung changes, and per-host next-request times. Read it with `Ledger(home, day).read()`.
- Every read-modify-write takes a file lock; every write is an atomic replace. Files carry `"version": 1`; a newer version fails loud (`StoreError`).
- Entry shape: `{"rung": ..., "won": "YYYY-MM-DD", "gap_s": 4.0}` — `gap_s` optional; pacing uses the larger of it and the session's gap. Set entries with `SiteStore(dir).put(host, rung=..., won=..., gap_s=...)`.

## Hosted readers

- On by default; `allow_hosted=False` / `--no-hosted` keeps a session local.
- **Cap: 30 hosted calls per UTC day per machine** (`HOSTED_CAP`; `Session(cap=...)`), counted in the day ledger across processes and stores. Cap spent: a fetch whose only remaining rungs are hosted fails loud (`kind="cap"`) and the saved rung stays.
- A reader's 402 or 429 stops **that reader** for the rest of the day (never the target host); the climb goes on to the next reader.
- A reader's answer counts only if it exposes the target's status and that status is 2xx; otherwise it fails closed and is never saved.
- Requests ask the reader not to serve from cache (Firecrawl `maxAge: 0`).

## Credential-shaped links never go to a hosted reader

A link that is itself an access credential stays on local rungs (explicit hosted rungs refuse it, `kind="refused"`). The list is exactly (switch-berlin sb-7wzb.103 D4; adding a shape is an edit to that decision):

- hosts `t.me`, `telegram.me`, `chat.whatsapp.com`, `signal.group`, `discord.gg`, `forms.gle`, `drive.google.com`; paths `discord.com/invite/*`, `docs.google.com/forms/*`;
- any URL with a query key named `token`, `key`, `code`, `invite`, `secret`, `sig`, `signature`, `access`, `auth` (case-insensitive).

## Vetting, every hop of every rung

http(s) only; no reader/proxy hosts or URL-in-URL shapes; the host's addresses (IP literal or every resolved address) must not be private, loopback, link-local, reserved, multicast or unspecified; an unresolvable host fails. Redirects are vetted hop by hop, a browser's landing URL too. The resolve-then-connect window (DNS rebinding) stays open.

Tests: `uv run pytest -q` (fakes only; no live network, no hosted reader).
