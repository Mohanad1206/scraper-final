# Web Scraper v17.1 — Unblocker-enabled (2025-10-05T18:27:18.289755+00:00Z)

**New:** Per-domain *provider* mode for heavy sites (Amazon/Noon/Jumia).
Supported providers: **ScrapingBee**, **ScraperAPI**, **Zyte API**.

## Configure
1. Add API key to repo **Secrets** (Settings → Secrets and variables → Actions → New repository secret):
   - `SCRAPINGBEE_API_KEY` or/and `SCRAPERAPI_KEY` or/and `ZYTE_API_KEY`
2. Edit `scraper/config.json` → `overrides["<domain>"].provider`:
```json
"provider": {
  "name": "scrapingbee",
  "key_env": "SCRAPINGBEE_API_KEY",
  "geo": "AE",
  "render_js": true,
  "timeout_ms": 120000
}
```
Supported `name` values: `"scrapingbee"`, `"scraperapi"`, `"zyte"`.

3. Ensure the domain is listed in `scraper/sites.txt`.

## How it fetches
Order of preference per domain:
- If `provider` is configured → fetch via provider (residential proxy + headless)
- Else if `render` is false → static `httpx`
- Else → Playwright (Chromium) with scroll/click loops + static fallback

## Output
- `out/snapshot.csv` and `out/snapshot.jsonl` (and committed to `data/` by CI)

## Notes
- Keep the price range and filters in `config.json` to control scope.
- If a provider key is missing, the scraper prints a clear error and falls back to Playwright/static when possible.
