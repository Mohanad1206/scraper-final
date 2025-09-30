import asyncio
import json
import os
import re
import sys
import time
import tldextract
import argparse
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_fixed
# --- DEFENSIVE_IMPORT: ensure package imports work when executed as a script ---
try:
    from scraper.utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore
except ModuleNotFoundError:
    # Fallback: add parent dir to sys.path and import relative
    import sys as _sys, os as _os
    _sys.path.append(_os.path.dirname(_os.path.abspath(__file__)))
    from utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore

# Playwright is optional until runtime so the module import doesn't break packaging.
from playwright.sync_api import sync_playwright

OUT_PATH = "out/snapshot.jsonl"
CONFIG_PATH = "scraper/config.json"
SITES_PATH = "scraper/sites.txt"

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_sites() -> List[str]:
    with open(SITES_PATH, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

def domain_of(url: str) -> str:
    ext = tldextract.extract(url)
    parts = [p for p in [ext.domain, ext.suffix] if p]
    return ".".join(parts) if parts else url

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def write_jsonl(records: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def normalize_text(s: Optional[str]) -> str:
    if not s: return ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def best_text(node, selectors: List[str]) -> str:
    for sel in selectors:
        el = node.select_one(sel)
        if el and normalize_text(el.get_text()):
            return normalize_text(el.get_text())
    return ""

def best_href(node, selectors: List[str]) -> str:
    for sel in selectors:
        el = node.select_one(sel)
        if el and el.has_attr("href"):
            return el["href"]
    # sometimes the card root itself is a link
    if node.name == "a" and node.has_attr("href"):
        return node["href"]
    return ""

def absolutize(base: str, href: str) -> str:
    if not href: return base
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        # derive origin
        m = re.match(r"^(https?://[^/]+)", base)
        if m:
            return m.group(1) + href
    # simple join
    if base.endswith("/") and href.startswith("/"):
        return base[:-1] + href
    if not base.endswith("/") and not href.startswith("/"):
        return base + "/" + href
    return base + href

def infer_availability(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["out of stock", "sold out", "غير متوفر", "غير متاح"]):
        return "Out of Stock"
    if any(w in t for w in ["in stock", "available", "متاح", "متوفر"]):
        return "Available"
    return "Unknown"

def scrape_with_playwright(url: str, timeout_ms: int = 45000) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        # gentle scroll to load lazy content
        for _ in range(5):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(600)
        html = page.content()
        context.close()
        browser.close()
        return html

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
def fetch_static(url: str, timeout=20) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WebScraperBot/1.0)"}
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.text

def extract_products(html: str, base_url: str, site_label: str, override: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    records = []

    cards = []
    used = "heuristic"
    if override and override.get("product_card"):
        for sel in override["product_card"]:
            got = soup.select(sel)
            if len(got) >= 3:
                cards = got
                used = "override"
                break
    if not cards:
        cards = heuristic_cards(soup)

    for card in cards[:250]:  # hard upper bound defensively
        # NAME
        name = ""
        if override and override.get("name"):
            name = best_text(card, override["name"])
        if not name:
            # fallback generic
            name = best_text(card, ["[itemprop='name']", ".product-title", ".product-name", "h3 a", "h2 a", "h3", "h2", "a"])

        # URL
        url = ""
        if override and override.get("url"):
            url = best_href(card, override["url"])
        if not url:
            url = best_href(card, ["a[href]"])
        url = absolutize(base_url, url)

        # PRICE
        price_text = best_text(card, [".price", ".price .amount", ".price .money", ".price-wrapper .price", ".Price .money", ".current-price", "[itemprop='price']"])
        price_val, raw_price = parse_price(price_text if price_text else card.get_text(" ", strip=True))

        # CURRENCY
        currency = detect_currency(price_text) or "EGP"

        # AVAILABILITY
        status = infer_availability(card.get_text(" ", strip=True))

        if not name and not price_val and url == base_url:
            # Too noisy card; skip
            continue

        rec = {
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "site_name": site_label,
            "product_name": name,
            "sku": "",
            "product_url": url,
            "status": status,
            "price_value": price_val if price_val is not None else "",
            "currency": currency,
            "raw_price_text": raw_price,
            "source_url": base_url,
            "notes": used
        }
        records.append(rec)

    return records

def scrape_site(site_url: str, cfg: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    site_dom = domain_of(site_url)
    override = cfg.get("overrides", {}).get(site_dom)
    timeout_ms = cfg.get("limits", {}).get("timeout_ms", 45000)

    # First try dynamic
    html = ""
    try:
        html = scrape_with_playwright(site_url, timeout_ms=timeout_ms)
    except Exception as e:
        print(f"[{site_dom}] Playwright failed: {e}. Falling back to static...")

    if not html:
        try:
            html = fetch_static(site_url)
        except Exception as e:
            print(f"[{site_dom}] Static fetch failed: {e}")
            return []

    products = extract_products(html, site_url, site_dom, override)
    if not products:
        print(f"[{site_dom}] No products detected on landing page; recorded 0.")
        return []

    # Cap to limit
    return products[:limit]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="Max products per site")
    args = ap.parse_args()

    cfg = load_config()
    sites = load_sites()
    all_records = []
    for site in sites:
        print(f"Scraping: {site}")
        try:
            recs = scrape_site(site, cfg, args.limit)
            print(f"  -> got {len(recs)} products")
            if recs:
                write_jsonl(recs)
                all_records.extend(recs)
        except Exception as e:
            print(f"Error scraping {site}: {e}")

    if not all_records:
        # Ensure we still create a file to debug downstream steps
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write("")  # empty file marker
        print("Finished with no records. See logs; consider adding overrides in scraper/config.json.")

if __name__ == "__main__":
    main()
