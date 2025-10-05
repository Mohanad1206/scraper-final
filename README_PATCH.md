PATCH v16.3 — 2025-10-05T18:09:13.864369+00:00

Goal: **product_name should contain ONLY the name** (no "Regular price", no currency or numbers from the price).
What’s new:
- Clean name pipeline that strips price lines/tokens and currency-number pairs.
- Name extractor now removes price nodes inside heading before taking text.
- Safer fallback: prefers link/heading text; avoids container text that mixes price.

Drop-in replacements:
- scraper/utils/extract.py
- scraper/run.py (tiny hook to call `clean_product_name`)

No output schema change.
