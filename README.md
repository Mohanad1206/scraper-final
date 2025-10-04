# Web Scraper — v16
- **Availability rule:** default to **Available** unless the card text explicitly says **Out of Stock** (Arabic cues supported).
- **Accessories-only** with broad keywords + URL-path heuristic.
- **Price range** filter: 100–2000 EGP (parsed numeric).
- **Unlimited output** when run with `--limit 0` (workflow uses this).
- JSON-LD + attribute fallbacks for names; per-site adapters + seeds; static/dynamic per domain with fallback.
- Outputs CSV and JSONL -> committed to `data/` or PR if protected.
