
import re
from bs4 import BeautifulSoup

PRICE_REGEX = re.compile(r'(?i)(?:EGP|ج\.م|جنيه|جنيه مصري|LE|E£|£E|L\.E\.?)\s*([\d\.,]+)|([\d\.,]+)\s*(?:EGP|ج\.م|جنيه|جنيه مصري|LE|E£|£E|L\.E\.?)')
CURRENCY_NEAR_NUMBER = re.compile(r'(?i)(?:EGP|LE|E£|£E|L\.E\.?|ج\.م|جنيه)\s*[\d\.,]+|[\d\.,]+\s*(?:EGP|LE|E£|£E|L\.E\.?|ج\.م|جنيه)')
PRICE_LINE_TOKENS = re.compile(r'(?i)^(regular|sale)\s+price.*')

def detect_currency(text: str) -> str:
    if not text:
        return ""
    if any(sym in text for sym in ["EGP", "LE", "E£", "£E", "L.E", "ج.م", "جنيه"]):
        return "EGP"
    return ""

def parse_price(text: str):
    if not text:
        return None, ""
    raw = text.strip()
    m = PRICE_REGEX.search(raw)
    if not m:
        return None, raw
    num = m.group(1) or m.group(2)
    if not num:
        return None, raw
    try:
        clean = num.replace(",", "").replace(" ", "")
        if clean.count(".") > 1:
            clean = clean.replace(".", "", clean.count(".") - 1)
        return float(clean), raw
    except Exception:
        return None, raw

def _first_attr(node, attrs):
    for a in attrs:
        if hasattr(node, "has_attr") and node.has_attr(a) and node[a]:
            return node[a]
    return ""

def name_from_node(card):
    # Fallback that tries several headings/links but may include prices
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

def _strip_price_nodes(el):
    # Remove nested price-like nodes to avoid polluting the name
    if not el:
        return
    for sel in [
        ".price", ".price .amount", ".money", ".woocommerce-Price-amount",
        ".woocommerce-Price-amount bdi", ".current-price", ".Price", "[aria-hidden='true']"
    ]:
        for n in el.select(sel):
            n.decompose()

def clean_product_name(card, preferred_selectors=None) -> str:
    """
    Return heading/link text without any price fragments.
    """
    preferred_selectors = preferred_selectors or []
    # Try preferred selectors first
    for sel in preferred_selectors:
        el = card.select_one(sel)
        if el:
            el = el.clone() if hasattr(el, "clone") else el
            _strip_price_nodes(el)
            txt = el.get_text(" ", strip=True)
            txt = _cleanup_text(txt)
            if txt:
                return txt

    # Otherwise search a few generic heading spots
    for sel in [".product-title a", ".product-title", ".product-name a", ".product-name", "[itemprop='name']", "h3 a", "h2 a", "h3", "h2", "a"]:
        el = card.select_one(sel)
        if el:
            el = el.clone() if hasattr(el, "clone") else el
            _strip_price_nodes(el)
            txt = el.get_text(" ", strip=True)
            txt = _cleanup_text(txt)
            if txt:
                return txt

    # Fallback: whole card text, cleaned
    txt = card.get_text(" ", strip=True)
    return _cleanup_text(txt)

def _cleanup_text(txt: str) -> str:
    if not txt:
        return ""
    # remove separate "Regular price ..." or "Sale price ..." lines
    parts = [p for p in re.split(r'\s{2,}|\n', txt) if p]
    parts = [p for p in parts if not PRICE_LINE_TOKENS.match(p)]
    txt = " ".join(parts)

    # remove currency near numbers
    txt = CURRENCY_NEAR_NUMBER.sub("", txt)

    # compact spaces
    txt = re.sub(r"\s{2,}", " ", txt).strip(" -–—\t ")
    # Avoid empty after cleaning
    return txt.strip()
