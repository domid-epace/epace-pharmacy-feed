#!/usr/bin/env python3
"""
generate_feed.py - builds feed.xml, a Google-Shopping-style RSS 2.0 product
feed for the ePACE Pharmacy mock Shopify store (team-epace.myshopify.com),
for the Bloomreach AI Hackathon 2026.

Stdlib only (urllib, xml handling done via careful string building so the
output format - and the "only rewrite on real changes" diff behaviour - is
fully under our control).

Env vars:
  SHOPIFY_SHOP            default "team-epace" (bare shop name, no domain)
  SHOPIFY_CLIENT_ID       required - Dev Dashboard app client id
  SHOPIFY_CLIENT_SECRET   required - Dev Dashboard app client secret
  SHOPIFY_API_VERSION     default "2026-07"

Auth: OAuth2 client_credentials grant against
  POST https://{shop}.myshopify.com/admin/oauth/access_token
(same request shape epace-shopify-mcp's shopify_client.py uses to get its
Admin API app token). The resulting token is then sent as
X-Shopify-Access-Token on Admin GraphQL calls.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import html as htmllib
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
import urllib.request
import urllib.error

# ── config ───────────────────────────────────────────────────────────────

SHOP = os.environ.get("SHOPIFY_SHOP", "team-epace").strip().replace(".myshopify.com", "")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07").strip()

SHOP_DOMAIN = f"{SHOP}.myshopify.com"
BASE_URL = f"https://{SHOP_DOMAIN}"
STOREFRONT_URL = f"https://{SHOP_DOMAIN}"
USER_AGENT = "epace-pharmacy-feed/1.0 (+ePACE Bloomreach AI Hackathon 2026)"

FEED_PATH = Path(__file__).resolve().parent / "feed.xml"

# NOTE on currency: the Shopify Admin API always returns money amounts in the
# shop's underlying base currency, which for team-epace is GBP. However the
# published storefront's presentation currency is fixed to EUR at a 1.0
# conversion rate (a deliberate hackathon-demo setting - see the project
# notes), so the numeric amount the Admin API returns is *also* the correct
# EUR amount shown to shoppers. We therefore label every price "EUR" without
# doing any conversion math - converting would silently double up the rate.
CURRENCY_CODE = "EUR"

GOOGLE_NS = "http://base.google.com/ns/1.0"
EPACE_NS = "https://epace.cz/ns/pharmacy-feed/1.0"

PHARMACY_COLLECTION_HANDLE = "pharmacy"


class FeedError(Exception):
    pass


# ── HTTP / GraphQL plumbing (mirrors shopify_client.py's app-token flow) ──

def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          method: str = "GET", timeout: int = 90) -> tuple[int, bytes]:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _check_env():
    if not (SHOP and CLIENT_ID and CLIENT_SECRET):
        missing = [n for n, v in (("SHOPIFY_CLIENT_ID", CLIENT_ID),
                                   ("SHOPIFY_CLIENT_SECRET", CLIENT_SECRET)) if not v]
        raise FeedError(
            "Missing required credentials: " + ", ".join(missing or ["SHOPIFY_SHOP"]) +
            ". Set them as environment variables (in CI: repository secrets "
            "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET) before running generate_feed.py.")


def get_access_token() -> str:
    """OAuth2 client_credentials grant -> short-lived Admin API app token."""
    _check_env()
    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    status, body = _http(
        f"{BASE_URL}/admin/oauth/access_token", form,
        {"Content-Type": "application/x-www-form-urlencoded"}, "POST", 30)
    if status != 200:
        raise FeedError(f"Token request failed ({status}): {body.decode(errors='replace')[:500]}")
    data = json.loads(body.decode())
    token = data.get("access_token")
    if not token:
        raise FeedError(f"No access_token in token response: {data}")
    return token


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    url = f"{BASE_URL}/admin/api/{API_VERSION}/graphql.json"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    status, body = None, None
    for attempt in range(4):
        status, body = _http(url, payload, headers, "POST", 120)
        if status == 429 or status >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    text = body.decode(errors="replace")
    if status != 200:
        raise FeedError(f"GraphQL request failed ({status}): {text[:1500]}")
    result = json.loads(text)
    errs = result.get("errors")
    if errs:
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs if isinstance(e, dict)):
            time.sleep(2)
            return graphql(token, query, variables)
        raise FeedError(f"GraphQL errors: {json.dumps(errs, ensure_ascii=False)[:2000]}")
    return result.get("data") or {}


# ── fetching products ───────────────────────────────────────────────────

PRODUCTS_QUERY = """
query Products($first: Int!, $after: String) {
  products(first: $first, after: $after, query: "tag:pharmacy status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      title
      descriptionHtml
      vendor
      productType
      tags
      featuredMedia { preview { image { url } } }
      media(first: 5) {
        nodes { ... on MediaImage { image { url } } }
      }
      variants(first: 1) {
        nodes { sku price compareAtPrice inventoryQuantity }
      }
      collections(first: 10) { nodes { handle title } }
      units_per_pack: metafield(namespace: "refill", key: "units_per_pack") { value }
      form: metafield(namespace: "refill", key: "form") { value }
      pil: metafield(namespace: "pharmacy", key: "pil") {
        reference { ... on GenericFile { url } }
      }
    }
  }
}
"""


def fetch_all_products(token: str) -> list[dict]:
    nodes: list[dict] = []
    after = None
    while True:
        data = graphql(token, PRODUCTS_QUERY, {"first": 50, "after": after})
        block = data["products"]
        nodes.extend(block["nodes"])
        if block["pageInfo"]["hasNextPage"]:
            after = block["pageInfo"]["endCursor"]
        else:
            break
    return nodes


# ── shaping a product node into a feed item ────────────────────────────

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def html_to_plain_text(html_text: str | None) -> str:
    if not html_text:
        return ""
    text = TAG_RE.sub(" ", html_text)
    text = htmllib.unescape(text)
    return WS_RE.sub(" ", text).strip()


def numeric_id(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def format_money(amount: str | float | None) -> str | None:
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return f"{value:.2f} {CURRENCY_CODE}"


def collect_images(node: dict) -> list[str]:
    urls: list[str] = []
    featured = ((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
    if featured.get("url"):
        urls.append(featured["url"])
    for m in (node.get("media") or {}).get("nodes") or []:
        img = (m or {}).get("image") or {}
        if img.get("url") and img["url"] not in urls:
            urls.append(img["url"])
    return urls[:5]


def pick_category(node: dict) -> tuple[str, str]:
    """Return (handle, title) of the collection that is NOT the blanket
    'pharmacy' collection - i.e. the specific pharmacy sub-category."""
    for c in (node.get("collections") or {}).get("nodes") or []:
        handle = c.get("handle") or ""
        if handle and handle != PHARMACY_COLLECTION_HANDLE:
            return handle, c.get("title") or handle
    return "", ""


def shape_item(node: dict) -> dict | None:
    variants = (node.get("variants") or {}).get("nodes") or []
    variant = variants[0] if variants else {}
    sku = (variant.get("sku") or "").strip()
    if not sku:
        print(f"WARNING: product {node.get('handle')} ({node.get('id')}) has no SKU - skipping.",
              file=sys.stderr)
        return None

    price = variant.get("price")
    compare_at = variant.get("compareAtPrice")
    price_f = float(price) if price not in (None, "") else 0.0
    compare_f = float(compare_at) if compare_at not in (None, "") else None

    if compare_f is not None and compare_f > price_f:
        g_price = format_money(compare_f)
        g_sale_price = format_money(price_f)
    else:
        g_price = format_money(price_f)
        g_sale_price = None

    qty = variant.get("inventoryQuantity")
    qty = int(qty) if qty is not None else 0
    availability = "in_stock" if qty > 0 else "out_of_stock"

    images = collect_images(node)
    category_handle, category_title = pick_category(node)
    product_type = f"Pharmacy > {category_title}" if category_title else "Pharmacy"

    units_val = ((node.get("units_per_pack") or {}) or {}).get("value")
    form_val = ((node.get("form") or {}) or {}).get("value")
    pil_ref = (((node.get("pil") or {}) or {}).get("reference") or {}) if node.get("pil") else {}
    pil_url = pil_ref.get("url")

    tags = node.get("tags") or []

    return {
        "sku": sku,
        "id": numeric_id(node["id"]),
        "handle": node["handle"],
        "title": node.get("title") or "",
        "description": html_to_plain_text(node.get("descriptionHtml")),
        "images": images,
        "g_price": g_price,
        "g_sale_price": g_sale_price,
        "availability": availability,
        "vendor": node.get("vendor") or "",
        "product_type": product_type,
        "category_handle": category_handle,
        "units_per_pack": units_val,
        "form": form_val,
        "pil_url": pil_url,
        "stock": qty,
        "tags": tags,
    }


# ── XML building (manual string assembly, deterministic + diff-friendly) ─

def esc(value) -> str:
    """XML-escape text content (not attribute-safe, we don't use attrs)."""
    s = "" if value is None else str(value)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def render_item(item: dict) -> str:
    lines = ["  <item>"]
    lines.append(f"    <g:id>{esc(item['sku'])}</g:id>")
    lines.append(f"    <title>{esc(item['title'])}</title>")
    lines.append(f"    <description>{esc(item['description'])}</description>")
    lines.append(f"    <link>{esc(STOREFRONT_URL + '/products/' + item['handle'])}</link>")

    if item["images"]:
        lines.append(f"    <g:image_link>{esc(item['images'][0])}</g:image_link>")
        for extra in item["images"][1:]:
            lines.append(f"    <g:additional_image_link>{esc(extra)}</g:additional_image_link>")

    lines.append(f"    <g:price>{esc(item['g_price'])}</g:price>")
    if item["g_sale_price"]:
        lines.append(f"    <g:sale_price>{esc(item['g_sale_price'])}</g:sale_price>")

    lines.append(f"    <g:availability>{item['availability']}</g:availability>")
    lines.append(f"    <g:brand>{esc(item['vendor'])}</g:brand>")
    lines.append("    <g:condition>new</g:condition>")
    lines.append(f"    <g:product_type>{esc(item['product_type'])}</g:product_type>")
    lines.append("    <g:identifier_exists>no</g:identifier_exists>")

    lines.append(f"    <epace:shopify_product_id>{esc(item['id'])}</epace:shopify_product_id>")
    if item["category_handle"]:
        lines.append(f"    <epace:category>{esc(item['category_handle'])}</epace:category>")
    if item["units_per_pack"] not in (None, ""):
        lines.append(f"    <epace:units_per_pack>{esc(item['units_per_pack'])}</epace:units_per_pack>")
    if item["form"]:
        lines.append(f"    <epace:form>{esc(item['form'])}</epace:form>")
    if item["pil_url"]:
        lines.append(f"    <epace:pil_url>{esc(item['pil_url'])}</epace:pil_url>")
    lines.append(f"    <epace:stock>{item['stock']}</epace:stock>")
    if item["tags"]:
        lines.append(f"    <epace:tags>{esc(', '.join(item['tags']))}</epace:tags>")

    lines.append("  </item>")
    return "\n".join(lines)


def render_feed(items: list[dict], build_date: str) -> str:
    items_sorted = sorted(items, key=lambda it: it["sku"])
    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="{GOOGLE_NS}" xmlns:epace="{EPACE_NS}">
<channel>
  <title>ePACE Pharmacy product feed</title>
  <link>{STOREFRONT_URL}</link>
  <description>Hackathon demo feed for ePACE's Bloomreach AI Hackathon 2026 submission. Auto-generated from the team-epace.myshopify.com mock pharmacy catalogue (products tagged "pharmacy") and refreshed daily at 09:00 Europe/Prague by a GitHub Action.</description>
  <lastBuildDate>{build_date}</lastBuildDate>
"""
    body = "\n".join(render_item(it) for it in items_sorted)
    footer = "\n</channel>\n</rss>\n"
    return header + body + "\n" + footer


NORMALIZE_BUILD_DATE_RE = re.compile(r"<lastBuildDate>.*?</lastBuildDate>")


def normalize(xml_text: str) -> str:
    """Strip the one field (lastBuildDate) that legitimately changes every
    run, so we can tell whether the actual item content changed."""
    return NORMALIZE_BUILD_DATE_RE.sub("<lastBuildDate/>", xml_text)


# ── main ─────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        token = get_access_token()
        nodes = fetch_all_products(token)
    except FeedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    items = []
    for node in nodes:
        shaped = shape_item(node)
        if shaped:
            items.append(shaped)

    build_date = format_datetime(datetime.now(timezone.utc))
    new_xml = render_feed(items, build_date)

    if FEED_PATH.exists():
        old_xml = FEED_PATH.read_text(encoding="utf-8")
        if normalize(old_xml) == normalize(new_xml):
            print(f"No item-level changes for {len(items)} products; "
                  f"feed.xml left untouched (lastBuildDate not bumped).")
            return 0

    FEED_PATH.write_text(new_xml, encoding="utf-8")
    print(f"Wrote {FEED_PATH} with {len(items)} items "
          f"({len(nodes)} products matched tag:pharmacy status:active).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
