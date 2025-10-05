
import argparse, json, os, re, traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import httpx
import tldextract
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_fixed
from playwright.sync_api import sync_playwright

try:
    from scraper.utils.extract import (
        heuristic_cards, detect_currency, parse_price, name_from_node, clean_product_name
    )  # type: ignore
except ModuleNotFoundError:
    import sys as _sys, os as _os
    _sys.path.append(_os.path.dirname(_os.path.abspath(__file__)))
    from utils.extract import (
        heuristic_cards, detect_currency, parse_price, name_from_node, clean_product_name
    )  # type: ignore

OUT_DIR = "out"
OUT_JSONL = os.path.join(OUT_DIR, "snapshot.jsonl")
OUT_CSV = os.path.join(OUT_DIR, "snapshot.csv")
CONFIG_PATH = "scraper/config.json"
SITES_PATH = "scraper/sites.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

FIELDS = ["timestamp_iso","site_name","product_name","sku","product_url","status","price_value","currency","raw_price_text","source_url","notes"]

def _strip_json_comments(s: str) -> str:
    s = re.sub(r'//.*', '', s)
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    return s

def _as_dict(x, default=None):
    return x if isinstance(x, dict) else ({} if default is None else default)

def _as_list(x):
    return x if isinstance(x, list) else []

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    raw = open(CONFIG_PATH, "r", encoding="utf-8").read()
    cleaned = _strip_json_comments(raw).strip()
    cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
    try:
        data = json.loads(cleaned)
    except Exception as e:
        print("[config] parse error:", e, flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    data["filters"] = _as_dict(data.get("filters"), {})
    data["overrides"] = _as_dict(data.get("overrides"), {})
    data["limits"] = _as_dict(data.get("limits"), {})
    return data

def load_sites() -> List[str]:
    if not os.path.exists(SITES_PATH):
        return []
    out = []
    for line in open(SITES_PATH, "r", encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.lstrip("-•* ").strip()
        if s and not s.startswith("http"):
            s = "https://" + s
        out.append(s)
    return out

def domain_of(url: str) -> str:
    ext = tldextract.extract(url)
    parts = [p for p in [ext.domain, ext.suffix] if p]
    return ".".join(parts) if parts else url

def same_domain(a: str, b: str) -> bool:
    try:
        return urlparse(a).netloc.split(":")[0].lower().endswith(urlparse(b).netloc.split(":")[0].lower())
    except Exception:
        return False

def write_jsonl(records: List[Dict[str, Any]]):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def write_csv(records: List[Dict[str, Any]]):
    os.makedirs(OUT_DIR, exist_ok=True)
    header_needed = not os.path.exists(OUT_CSV) or os.path.getsize(OUT_CSV) == 0
    import csv as _csv
    with open(OUT_CSV, "a", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=FIELDS)
        if header_needed:
            w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in FIELDS})

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
    if getattr(node, "name", "") == "a" and node.has_attr("href"):
        return node["href"]
    return ""

def infer_availability(text: str) -> str:
    t = (text or "").lower()
    oos_markers = ["out of stock", "sold out", "غير متوفر", "غير متاح", "نفدت الكمية", "out-of-stock", "unavailable"]
    if any(w in t for w in oos_markers):
        return "Out of Stock"
    return "Available"

def allowed_by_filters(name: str, url: str, cfg: Dict[str, Any], price: float | None = None) -> bool:
    name_l = (name or "").lower()
    url_l = (url or "").lower()
    filt = _as_dict(cfg.get("filters"), {})
    inc = [w.lower() for w in _as_list(filt.get("include_keywords"))]
    exc = [w.lower() for w in _as_list(filt.get("exclude_keywords"))]

    if any(w in name_l or w in url_l for w in exc):
        return False

    pf = _as_dict(cfg.get("price_filter"), {})
    try:
        lo = float(pf.get("min")) if pf.get("min") is not None else None
        hi = float(pf.get("max")) if pf.get("max") is not None else None
    except Exception:
        lo = hi = None
    if price is not None:
        if lo is not None and price < lo:
            return False
        if hi is not None and price > hi:
            return False

    hit = any(w in name_l or w in url_l for w in inc)
    if not hit:
        if any(p in url_l for p in [
            "/accessor", "/accessories", "/controllers", "/controller", "/keyboards",
            "/keyboard", "/mouse", "/mice", "/headset", "/headphone", "/audio",
            "/webcam", "/monitor", "/stands", "/mount", "/case", "/cooler", "/fans",
            "/cables", "/adapter", "/gaming-gear", "/gaming-accessories", "/peripherals"
        ]):
            hit = True
    if inc and not hit:
        return False
    return True

def scrape_with_playwright(url: str, timeout_ms: int = 60000) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(user_agent=UA, locale="en-US", timezone_id="Africa/Cairo")
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page.add_init_script("window.chrome = { runtime: {} };")
        page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});")
        page.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});")
        page.goto(url, wait_until="domcontentloaded")
        texts = ["Load more","Show more","View more","عرض المزيد","مشاهدة المزيد"]
        for _ in range(4):
            for t in texts:
                try:
                    page.get_by_text(t, exact=False).click(timeout=800)
                    page.wait_for_timeout(900)
                except Exception:
                    pass
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(900)
        html = page.content()
        context.close()
        browser.close()
        return html

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
def fetch_static(url: str, timeout=25) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": url,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.text

def canon_url(u: str) -> str:
    try:
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return (u or "").strip().rstrip("/")

def build_jsonld_name_map(soup) -> dict:
    mapping = {}
    try:
        scripts = soup.find_all("script", attrs={"type":"application/ld+json"})
        for s in scripts:
            raw = s.string or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            def handle(obj):
                if isinstance(obj, dict):
                    typ = obj.get("@type") or obj.get("type")
                    if typ in ("Product", "ListItem"):
                        url = obj.get("url") or (obj.get("item") or {}).get("@id") or (obj.get("item") or {}).get("url")
                        name = obj.get("name") or (obj.get("item") or {}).get("name")
                        if url and name:
                            mapping[canon_url(url)] = name
                    for v in obj.values():
                        handle(v)
                elif isinstance(obj, list):
                    for it in obj:
                        handle(it)
            handle(data)
    except Exception as e:
        print("[json-ld] warn:", e, flush=True)
    return mapping

def extract_products(html: str, base_url: str, site_label: str, override: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    name_map = build_jsonld_name_map(soup)
    records: List[Dict[str, Any]] = []
    if not isinstance(override, dict):
        override = {}

    cards = []
    used = "heuristic"
    if isinstance(override.get("product_card", None), list):
        for sel in override["product_card"]:
            got = soup.select(sel)
            if len(got) >= 3:
                cards = got
                used = "override"
                break
    if not cards:
        cards = heuristic_cards(soup)

    for card in cards[:1000]:
        name = ""
        if isinstance(override.get("name", None), list):
            name = clean_product_name(card, override["name"])
        if not name:
            name = clean_product_name(card, ["[itemprop='name']", ".product-title a", ".product-title", ".product-name a", ".product-name", "h3 a", "h2 a", "h3", "h2", "a"])
        if not name:
            name = clean_product_name(card, []) or name_from_node(card)

        url = ""
        if isinstance(override.get("url", None), list):
            url = best_href(card, override["url"])
        if not url:
            url = best_href(card, ["a[href]"])
        url = absolutize(base_url, url)
        if not name and url:
            nm = name_map.get(canon_url(url))
            if nm:
                name = nm

        price_text = best_text(card, [".price", ".price .amount", ".price .money", ".price-wrapper .price", ".Price .money", ".current-price", "[itemprop='price']", ".woocommerce-Price-amount bdi", ".woocommerce-Price-amount"])
        price_val, raw_price = parse_price(price_text if price_text else card.get_text(" ", strip=True))

        currency = detect_currency(price_text) or "EGP"
        status = "Available"  # default unless explicit out-of-stock tokens exist
        t = card.get_text(" ", strip=True).lower()
        if any(k in t for k in ["out of stock", "sold out", "غير متوفر", "غير متاح", "نفدت الكمية", "out-of-stock", "unavailable"]):
            status = "Out of Stock"

        pf = _as_dict(cfg.get("price_filter"), {})
        if (price_val is None) and (pf.get("min") is not None):
            continue

        if not allowed_by_filters(name, url, cfg, price_val if isinstance(price_val,(int,float)) else None):
            continue

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

def get_html_dynamic_then_static(url: str, timeout_ms: int) -> str:
    try:
        return scrape_with_playwright(url, timeout_ms=timeout_ms)
    except Exception:
        try:
            return fetch_static(url)
        except Exception:
            return ""

def scrape_site(site_url: str, cfg: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    site_dom = domain_of(site_url)
    overrides_raw = _as_dict(cfg.get("overrides"), {})
    override = _as_dict(overrides_raw.get(site_dom), {})

    timeout_ms = override.get("dynamic_timeout_ms", _as_dict(cfg.get("limits"), {}).get("timeout_ms", 60000))
    static_timeout = override.get("static_timeout_sec", 12)
    max_pages = override.get("per_site_pages", _as_dict(cfg.get("limits"), {}).get("per_site_pages", 10))
    prefer_static = (override.get("render") is False)

    visited: Set[str] = set()
    queue: List[str] = [site_url]

    for s in _as_list(override.get("seeds"))[:10]:
        try:
            u = urljoin(site_url, s)
            if u not in queue:
                queue.append(u)
        except Exception:
            pass

    all_records: List[Dict[str, Any]] = []
    unlimited = (limit == 0)

    def fetch(cur: str) -> str:
        if prefer_static:
            try:
                return fetch_static(cur, timeout=static_timeout)
            except Exception:
                pass
        return get_html_dynamic_then_static(cur, timeout_ms=timeout_ms)

    while queue and (unlimited or len(all_records) < limit) and len(visited) < 5000:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        try:
            html = fetch(cur)
            if not html:
                continue
            recs = extract_products(html, cur, site_dom, override, cfg)
            if prefer_static and not recs:
                html2 = get_html_dynamic_then_static(cur, timeout_ms=timeout_ms)
                if html2 and html2 != html:
                    recs = extract_products(html2, cur, site_dom, override, cfg)
            if recs:
                if not unlimited:
                    need = limit - len(all_records)
                    if need <= 0:
                        break
                    recs = recs[:need]
                write_jsonl(recs)
                write_csv(recs)
                all_records.extend(recs)
        except Exception as e:
            print(f"[{site_dom}] Error on {cur}: {e}", flush=True)
            traceback.print_exc()
            continue

        if max_pages > 0:
            soup = BeautifulSoup(html, "lxml")
            nexts = []
            selectors = [
                "a[rel='next']","link[rel='next']","a.next","a.pagination__next",
                "a.page-link[rel='next']","a[aria-label*='Next' i]",
                "a[href*='?page=']","a[href*='/page/']",
                "li.pagination-next a",".pagination a.next"
            ]
            for sel in selectors:
                for el in soup.select(sel):
                    href = el.get("href") or el.get("content")
                    if not href:
                        continue
                    u = urljoin(cur, href)
                    if urlparse(u).scheme.startswith("http") and same_domain(u, cur):
                        nexts.append(u)
            for nxt in nexts:
                if nxt not in visited and nxt not in queue and len(queue) < 60:
                    queue.append(nxt)
            max_pages -= 1

    return all_records if unlimited else all_records[:limit]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = unlimited")
    args = ap.parse_args()

    cfg = load_config()
    sites = load_sites()

    os.makedirs(OUT_DIR, exist_ok=True)
    for p in (OUT_JSONL, OUT_CSV):
        if os.path.exists(p):
            os.remove(p)

    for site in sites:
        try:
            print(f"Scraping: {site}", flush=True)
            scrape_site(site, cfg, args.limit)
        except Exception as e:
            print(f"Error scraping {site}: {e}", flush=True)
            traceback.print_exc()

    for p in (OUT_JSONL, OUT_CSV):
        if not os.path.exists(p):
            open(p, "w", encoding="utf-8").close()

if __name__ == "__main__":
    main()
