
# Web Scraper with Webhook — v7

- Multi-page crawl, sitemap discovery, category filtering (PC accessories + controllers only).
- Default per-site limit: **500** (override with `--limit N`).
- Outputs **JSONL** and **CSV**: `out/snapshot.jsonl` and `out/snapshot.csv`.
- GitHub Actions **commits** snapshots into `data/snapshot.jsonl` and `data/snapshot.csv` on the same branch.
- Artifacts are still uploaded for easy download.

## Google Sheets (live import from repo)
After one successful run (so the files exist in the repo), use:
```
=IMPORTDATA("https://raw.githubusercontent.com/<OWNER>/<REPO>/<BRANCH>/data/snapshot.csv")
```
Replace `<OWNER>`, `<REPO>`, and `<BRANCH>` (e.g., `main`).  
`IMPORTDATA` works with CSV; if you prefer JSON, use Apps Script or an add-on to parse `data/snapshot.jsonl`.

## Local run
```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python -m scraper.run --limit 500
```
