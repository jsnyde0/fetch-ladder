# fetch-ladder

Fetch one URL up a ladder of clients with shared manners: per-host pacing, HTTP 429 wait-out, challenge detection, reader/proxy URL vetting. Hosted readers never run unless `allow_hosted`.

Rungs: `httpx`, `urllib`, `curl_cffi:<target>`, `playwright` (not built), `hosted:jina`, `hosted:firecrawl` (keys `JINA_API_KEY`, `FIRECRAWL_API_KEY`).

    uvx --from ~/code/personal/fetch-ladder fetch-ladder URL [--rung R | --climb] [--allow-hosted] [--gap S] [--form BODY] [--json | --text]

    from fetch_ladder import Session
    page = Session().fetch(url, rung="urllib")   # Page(body, text, final_url, status, links, rung)
    page = Session().climb(url)                  # raises FetchFailed / FetchTransportError

Tests: `uv run pytest -q`.
