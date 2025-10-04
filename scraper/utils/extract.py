import re
from bs4 import BeautifulSoup

PRICE_REGEX = re.compile(r'(?i)(?:EGP|ج\.م|جنيه|جنيه مصري|LE|E£|£E|L\.E\.?)\s*([\d\.,]+)|([\d\.,]+)\s*(?:EGP|ج\.م|جنيه|جنيه مصري|LE|E£|£E|L\.E\.?)')

def detect_currency(text: str) -> str:
    if not text:
        return ""
    if any(sym in text for sym in ["EGP", "LE", "E£", "£E", "L.E", "ج.م", "جنيه"]):
        return "EGP"
    return ""

def parse_price(text: str):
    if not text:
        return None, ""
    m = PRICE_REGEX.search(text)
    raw = text.strip()
    if not m:
        return None, raw
    num = m.group(1) or m.group(2)
    if not num:
        return None, raw
    try:
        clean = num.replace(",", "").replace(" ", "")
        if clean.count(".") > 1:
            clean = clean.replace(".", "", clean.count(".") - 1)
        val = float(clean)
        return val, raw
    except Exception:
        return None, raw

def heuristic_cards(soup: BeautifulSoup):
    selectors = [
        "[data-product-id]",
        ".product-item",
        ".product-grid-item",
        ".product-card",
        ".grid__item",
        ".product",
        ".product-item-info",
        ".card-product",
        ".catalog-product",
        ".product-box",
        ".item.product",
        ".product-layout",
        ".product-thumb",
        "ul.products li.product"
    ]
    for sel in selectors:
        found = soup.select(sel)
        if len(found) >= 3:
            return found
    anchors = [a.parent for a in soup.select("a[href*='/product']")] + [a.parent for a in soup.select("a[href*='/products/']")]
    return anchors or []

def _first_attr(node, attrs):
    for a in attrs:
        if hasattr(node, "has_attr") and node.has_attr(a) and node[a]:
            return node[a]
    return ""

def name_from_node(card):
    txt_candidates = [
        ".card__heading a", ".card__heading", ".product-title a", ".product-title",
        "[itemprop='name']", "h3 a", "h2 a", "h3", "h2", "a"
    ]
    for sel in txt_candidates:
        el = card.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    for el in card.select("a, [data-product-title], [title], [aria-label]"):
        val = _first_attr(el, ["data-product-title", "title", "aria-label"])
        if val and val.strip():
            return val.strip()
    img = card.select_one("img[alt]")
    if img and img.get("alt"):
        return img["alt"].strip()
    return ""
