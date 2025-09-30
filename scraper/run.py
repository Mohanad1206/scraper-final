import asyncio
import json
import os
import re
import sys
import time
import tldextract
import argparse
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_fixed

# --- DEFENSIVE_IMPORT: ensure package imports work when executed as a script ---
try:
    from scraper.utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore
except ModuleNotFoundError:
    import sys as _sys, os as _os
    _sys.path.append(_os.path.dirname(_os.path.abspath(__file__)))
    from utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore

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

def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.split(":")[0].lower().endswith(urlparse(b).netloc.split(":")[0].lower())

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

def absolutize(base: str, href: str) -> str:
    if not href: return base
    return urljoin(base, href)

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
    if node.name == "a" and node.has_attr("href"):
        return node["href"]
    return ""

def infer_availability(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["out of stock", "sold out", "غير متوفر", "غير متاح"]):
        return "Out of Stock"
    if any(w in t for w in ["in stock", "available", "متاح", "متوفر"]):
        return "Available"
    return "Unknown"

def scrape_with_playwright(url: str, timeout_ms: int = 90000) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        # gentle scroll to load lazy content
        for _ in range(6):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(700)
        html = page.content()
        context.close()
        browser.close()
        return html

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
def fetch_static(url: str, timeout=25) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WebScraperBot/1.1)"}
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

    for card in cards[:400]:
        name = ""
        if override and override.get("name"):
            name = best_text(card, override["name"])
        if not name:
            name = best_text(card, ["[itemprop='name']", ".product-title", ".product-name", "h3 a", "h2 a", "h3", "h2", "a"])

        url = ""
        if override and override.get("url"):
            url = best_href(card, override["url"])
        if not url:
            url = best_href(card, ["a[href]"])
        url = absolutize(base_url, url)

        price_text = best_text(card, [".price", ".price .amount", ".price .money", ".price-wrapper .price", ".Price .money", ".current-price", "[itemprop='price']"])
        price_val, raw_price = parse_price(price_text if price_text else card.get_text(" ", strip=True))

        currency = detect_currency(price_text) or "EGP"
        status = infer_availability(card.get_text(" ", strip=True))

        if not name and not price_val and url == base_url:
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

def discover_listing_pages(html: str, base_url: str) -> List[str]:
    """Find likely product/category pages from any page HTML."""
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select("a[href]")
    cand: List[str] = []
    key_parts = [
        "/products", "/product", "/collections", "/catalog", "/category", "/categories",
        "/shop", "/store", "/gaming", "/game", "/accessor", "/acc", "/computers", "/pc"
    ]
    key_text = ["products", "shop", "store", "gaming", "accessor", "catalog", "browse"]
    for a in anchors:
        href = a.get("href", "")
        text = (a.get_text() or "").strip().lower()
        if any(k in href.lower() for k in key_parts) or any(t in text for t in key_text):
            url = absolutize(base_url, href)
            cand.append(url)
    # dedupe while keeping order and sticking to same domain
    seen: Set[str] = set()
    out = []
    for u in cand:
        if urlparse(u).scheme.startswith("http") and same_domain(u, base_url) and u not in seen:
            out.append(u); seen.add(u)
    return out[:8]

def find_next_pages(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    next_links = []
    # common next/pagination selectors
    sels = [
        "a[rel='next']",
        "link[rel='next']",
        "a.next", "a.pagination__next", "a.page-link[rel='next']",
        "a[aria-label*='Next' i]",
        "a[href*='?page=']", "a[href*='/page/']",
        "li.pagination-next a", ".pagination a.next"
    ]
    for sel in sels:
        for el in soup.select(sel):
            href = el.get("href") or el.get("content")
            if not href:
                continue
            url = absolutize(base_url, href)
            if url:
                next_links.append(url)
    # Deduplicate and same-domain filter
    seen: Set[str] = set()
    out = []
    for u in next_links:
        if urlparse(u).scheme.startswith("http") and same_domain(u, base_url) and u not in seen:
            out.append(u); seen.add(u)
    return out[:3]  # per listing page, limited

def get_html(url: str, timeout_ms: int) -> str:
    # Try dynamic first for coverage, fall back to static
    try:
        return scrape_with_playwright(url, timeout_ms=timeout_ms)
    except Exception as e:
        print(f"[dyn fail] {url}: {e}. Falling back to static.")
        try:
            return fetch_static(url)
        except Exception as e2:
            print(f"[static fail] {url}: {e2}")
            return ""

def scrape_site(site_url: str, cfg: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    site_dom = domain_of(site_url)
    override = cfg.get("overrides", {}).get(site_dom)
    timeout_ms = cfg.get("limits", {}).get("timeout_ms", 90000)
    max_pages = cfg.get("limits", {}).get("per_site_pages", 10)

    visited: Set[str] = set()
    queue: List[str] = [site_url]

    all_records: List[Dict[str, Any]] = []

    while queue and len(all_records) < limit and len(visited) < 100:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)

        html = get_html(cur, timeout_ms=timeout_ms)
        if not html:
            continue

        # Extract products from this page
        recs = extract_products(html, cur, site_dom, override)
        if recs:
            need = limit - len(all_records)
            if need <= 0:
                break
            recs = recs[:need]
            write_jsonl(recs)
            all_records.extend(recs)
            print(f"[{site_dom}] {cur} -> {len(recs)} products (total {len(all_records)}/{limit})")

        # Discover listing/category pages from this page (only early)
        if len(visited) <= 3:
            listings = discover_listing_pages(html, cur)
            for u in listings:
                if u not in visited and u not in queue and len(queue) < 20:
                    queue.append(u)

        # Add pagination from this page
        if max_pages > 0:
            nexts = find_next_pages(html, cur)
            for nxt in nexts[:max_pages]:
                if nxt not in visited and nxt not in queue and len(queue) < 20:
                    queue.append(nxt)
            max_pages -= 1

    return all_records[:limit]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="Max products per site")
    args = ap.parse_args()

    cfg = load_config()
    sites = load_sites()

    # Reset output each run
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    if os.path.exists(OUT_PATH):
        os.remove(OUT_PATH)

    all_records = []
    for site in sites:
        print(f"Scraping: {site}")
        try:
            recs = scrape_site(site, cfg, args.limit)
            print(f"  -> got {len(recs)} products for {site}")
            all_records.extend(recs)
        except Exception as e:
            print(f"Error scraping {site}: {e}")

    if not all_records and not os.path.exists(OUT_PATH):
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write("")

if __name__ == "__main__":
    main()
