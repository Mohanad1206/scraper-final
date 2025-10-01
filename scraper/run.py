
import asyncio
import csv
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

try:
    from scraper.utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore
except ModuleNotFoundError:
    import sys as _sys, os as _os
    _sys.path.append(_os.path.dirname(_os.path.abspath(__file__)))
    from utils.extract import heuristic_cards, detect_currency, parse_price  # type: ignore

from playwright.sync_api import sync_playwright

OUT_DIR = "out"
OUT_JSONL = os.path.join(OUT_DIR, "snapshot.jsonl")
OUT_CSV = os.path.join(OUT_DIR, "snapshot.csv")
CONFIG_PATH = "scraper/config.json"
SITES_PATH = "scraper/sites.txt"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

FIELDS = ["timestamp_iso","site_name","product_name","sku","product_url","status","price_value","currency","raw_price_text","source_url","notes"]

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

def write_jsonl(records: List[Dict[str, Any]]):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def write_csv(records: List[Dict[str, Any]]):
    os.makedirs(OUT_DIR, exist_ok=True)
    header_needed = not os.path.exists(OUT_CSV) or os.path.getsize(OUT_CSV) == 0
    with open(OUT_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if header_needed:
            w.writeheader()
        for r in records:
            row = {k: r.get(k, "") for k in FIELDS}
            w.writerow(row)

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

def allowed_by_filters(name: str, url: str, cfg: Dict[str, Any]) -> bool:
    name_l = (name or "").lower()
    url_l = (url or "").lower()
    filt = cfg.get("filters", {})
    inc = [w.lower() for w in filt.get("include_keywords", [])]
    exc = [w.lower() for w in filt.get("exclude_keywords", [])]
    if any(w in name_l or w in url_l for w in exc):
        return False
    if inc:
        return any(w in name_l or w in url_l for w in inc)
    return True

def scrape_with_playwright(url: str, timeout_ms: int = 120000) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=UA, locale="en-US", timezone_id="Africa/Cairo")
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page.add_init_script("window.chrome = { runtime: {} };")
        page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});")
        page.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});")
        page.goto(url, wait_until="domcontentloaded")
        texts = ["Load more","Show more","View more","عرض المزيد","مشاهدة المزيد"]
        for _ in range(5):
            for t in texts:
                try:
                    page.get_by_text(t, exact=False).click(timeout=1000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(800)
        html = page.content()
        context.close()
        browser.close()
        return html

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
def fetch_static(url: str, timeout=25) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WebScraperBot/1.2)"}
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.text

def extract_products(html: str, base_url: str, site_label: str, override: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
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

    for card in cards[:600]:
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

        if not allowed_by_filters(name, url, cfg):
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

def discover_category_links_by_text(html: str, base_url: str, cfg: Dict[str, Any]) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select("a[href]")
    inc = [w.lower() for w in cfg.get("filters", {}).get("include_keywords", [])]
    words = set(inc + ["accessor","gaming","keyboard","mouse","headset","controller","gamepad","webcam","microphone","monitor","stand","mount","arm","dock","usb","cable","rgb","chair","pad","mat"])
    out = []
    seen = set()
    for a in anchors:
        text = (a.get_text() or "").strip().lower()
        href = a.get("href","")
        if not href: 
            continue
        if any(w in text for w in words) or any(w in href.lower() for w in words):
            url = absolutize(base_url, href)
            if urlparse(url).scheme.startswith("http") and same_domain(url, base_url) and url not in seen:
                out.append(url); seen.add(url)
    return out[:12]

def try_fetch_sitemap_urls(base: str) -> List[str]:
    candidates = ["/sitemap.xml","/sitemap_index.xml","/sitemap-index.xml","/sitemap-products.xml","/sitemap_products_1.xml"]
    found = []
    for c in candidates:
        url = urljoin(base, c)
        try:
            xml = fetch_static(url)
        except Exception:
            continue
        try:
            root = BeautifulSoup(xml, "xml")
            for loc in root.find_all("loc"):
                u = loc.get_text(strip=True)
                if any(k in u for k in ["/product","/products","/category","/collections","/catalog"]):
                    if same_domain(u, base):
                        found.append(u)
        except Exception:
            pass
    seen = set(); out = []
    for u in found:
        if u not in seen:
            out.append(u); seen.add(u)
    return out[:50]

def find_next_pages(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    next_links = []
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
    seen = set(); out = []
    for u in next_links:
        if urlparse(u).scheme.startswith("http") and same_domain(u, base_url) and u not in seen:
            out.append(u); seen.add(u)
    return out[:3]

def get_html(url: str, timeout_ms: int) -> str:
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
    timeout_ms = cfg.get("limits", {}).get("timeout_ms", 120000)
    max_pages = cfg.get("limits", {}).get("per_site_pages", 15)

    visited: Set[str] = set()
    queue: List[str] = [site_url]

    all_records: List[Dict[str, Any]] = []

    try:
        sm = try_fetch_sitemap_urls(site_url)
        for u in sm[:10]:
            if u not in queue: queue.append(u)
    except Exception:
        pass

    while queue and len(all_records) < limit and len(visited) < 250:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)

        html = get_html(cur, timeout_ms=timeout_ms)
        if not html:
            continue

        recs = extract_products(html, cur, site_dom, override, cfg)
        if recs:
            need = limit - len(all_records)
            if need <= 0:
                break
            recs = recs[:need]
            write_jsonl(recs)
            write_csv(recs)
            all_records.extend(recs)
            print(f"[{site_dom}] {cur} -> {len(recs)} products (total {len(all_records)}/{limit})")

        if len(visited) <= 3:
            cats = discover_category_links_by_text(html, cur, cfg)
            for u in cats:
                if u not in visited and u not in queue and len(queue) < 30:
                    queue.append(u)

        if max_pages > 0:
            nexts = find_next_pages(html, cur)
            for nxt in nexts[:max_pages]:
                if nxt not in visited and nxt not in queue and len(queue) < 30:
                    queue.append(nxt)
            max_pages -= 1

    return all_records[:limit]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500, help="Max products per site")
    args = ap.parse_args()

    cfg = load_config()
    sites = load_sites()

    os.makedirs(OUT_DIR, exist_ok=True)
    for p in (OUT_JSONL, OUT_CSV):
        if os.path.exists(p):
            os.remove(p)

    all_records = []
    for site in sites:
        print(f"Scraping: {site}")
        try:
            recs = scrape_site(site, cfg, args.limit)
            print(f"  -> got {len(recs)} products for {site}")
            all_records.extend(recs)
        except Exception as e:
            print(f"Error scraping {site}: {e}")

    # If still no files, create empty ones so downstream steps exist
    for p in (OUT_JSONL, OUT_CSV):
        if not os.path.exists(p):
            open(p, "w", encoding="utf-8").close()

if __name__ == "__main__":
    main()
