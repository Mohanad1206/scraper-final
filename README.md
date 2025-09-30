# Web Scraper with Webhook — Fixed

A robust scraper that targets dynamic and static sites, outputs **newline-delimited JSON (.jsonl)**, and (optionally) POSTs the results to a **Make.com** webhook.

## What’s fixed
- Guaranteed **text output** in `out/snapshot.jsonl` (even when only partial fields are found).
- **Heuristic extraction** + optional per-domain **selector overrides** to avoid empty results.
- **GitHub Actions** workflow that installs Playwright (with Chromium) properly, uploads artifacts, and only fires the webhook if the file is **non-empty**.
- Clear logs. If a site yields nothing, we log **why** and still continue to the next site.

## Output Schema (JSONL, one line per product)
Each line is a JSON object with these keys:

- `timestamp_iso` (UTC ISO8601)
- `site_name`
- `product_name`
- `sku` (may be blank if not found)
- `product_url`
- `status` (Available/Out of Stock/Unknown)
- `price_value` (float or int if parsed; else blank)
- `currency` (defaults to "EGP" if unknown)
- `raw_price_text` (original text seen)
- `source_url` (listing page or site url)
- `notes` (e.g., "heuristic", "override:shamystores", or error info)

## Local Run (first time)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python -m scraper.run --limit 50
```

The output will be at `out/snapshot.jsonl`.

## Configure Target Sites
- Add or edit URLs in `scraper/sites.txt`
- (Optional) Add per-domain CSS overrides in `scraper/config.json`

## GitHub Actions (workflow)
- File: `.github/workflows/scrape.yml`
- Manually trigger: **Actions → "Scrape & Post" → Run workflow**
- Optional secret: `MAKE_WEBHOOK_URL` (Make.com webhook URL). If set, the workflow will POST the file only when non-empty.

## Notes
- This repo uses a **resilient heuristic** to find product cards and fields. You can gradually improve coverage by adding domain-specific overrides under `overrides` in `config.json`.
- If a site blocks headless browsing, try enabling `--slow` or increase timeouts.
- To cap products per site, use `--limit` (default: 50).


This version crawls listing pages and pagination and targets up to 200 products per site.
