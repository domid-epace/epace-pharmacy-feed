#!/usr/bin/env python3
"""
generate_feed.py - builds feed.xml, a Google-Shopping-style RSS 2.0 product
feed, for the ePACE Pharmacy mock Shopify store (Bloomreach AI Hackathon
2026 demo).

Why this exists: the dev storefront is password-protected and Shopify's
file/CDN URLs are cached for a year, so a feed hosted on Shopify itself is
not viable as a public, freshly-refreshed source. This script instead reads
the catalog straight from the Shopify Admin GraphQL API and writes a static
feed.xml that a GitHub Action commits to this repo once a day, served over
raw.githubusercontent.com.

Auth mirrors epace-shopify-mcp's shopify_client.py: an OAuth2
client_credentials grant against
  POST https://{shop}.myshopify.com/admin/oauth/access_token
    client_id=... client_secret=... grant_type=client_credentials
returns a short-lived Admin API access token.

Env vars:
  SHOPIFY_SHOP            default "team-epace" (no ".myshopify.com" suffix)
  SHOPIFY_CLIENT_ID       required
  SHOPIFY_CLIENT_SECRET   required
  SHOPIFY_API_VERSION     default "2026-07"

Stdlib only - no requests, no Shopify SDK.
"""

import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlencode
import urllib.request
import urllib.error

SHOP = os.environ.get("SHOPIFY_SHOP", "team-epace").strip().replace(".myshopify.com", "")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07").strip()

SHOP_DOMAIN = f"{SHOP}.myshopify.com"
BASE_URL = f"https://{SHOP_DOMAIN}"
USER_AGENT = "epace-pharmacy-feed/1.0 (+ePACE Bloomreach AI Hackathon 2026)"

# tag "category:<handle>" -> Google product_type leaf under "Pharmacy > ..."
CATEGORY_MAP = {
    "pain-fever": "Pain & Fever",
    "cold-flu": "Cold & Flu",
    "allergy": "Allergy",
    "vitamins-minerals": "Vitamins & Minerals",
    "heart-circulation": "Heart & Circulation",
    "digestive-health": "Digestive Health",
    "joints-muscles": "Joints & Muscles",
    "sleep-stress": "Sleep & Stress",
    "eye-care": "Eye Care",
}

# Technical tags used for merchandising/logic on the store, not shown to feed
# consumers via epace:tags.
TECHNICAL_TAGS = {"pharmacy", "has-pil"}

PRODUCTS_QUERY = """
query($first: Int!, $after: String) {
  products(first: $first, after: $after, query: "tag:pharmacy status:active") {
    nodes {
      id
      handle
      title
      vendor
      productType
      descriptionHtml
      tags
      images(first: 10) { nodes { url } }
      variants(first: 5) {
        nodes { sku price compareAtPrice inventoryQuantity }
      }
      metafields(first: 10, namespace: "refill") {
        nodes { key value }
      }
      pil: metafield(namespace: "pharmacy", key: "pil") {
        reference { ... on GenericFile { url } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class FeedError(Exception):
    pass


# ── HTTP / auth (mirrors shopify_client.py) ────────────────────────────────

def _http(url, data=None, headers=None, method="GET", timeout=60):
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_access_token() -> str:
    if not (SHOP and CLIENT_ID and CLIENT_SECRET):
        raise FeedError(
            "SHOPIFY_SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET must all be set "
            "(env vars or GitHub Actions secrets)."
        )
    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    status, body = _http(
        f"{BASE_URL}/admin/oauth/access_token", form,
        {"Content-Type": "application/x-www-form-urlencoded"}, "POST", 30,
    )
    if status != 200:
        raise FeedError(f"Token request failed ({status}): {body.decode(errors='replace')[:500]}")
    data = json.loads(body.decode())
    token = data.get("access_token")
    if not token:
        raise FeedError(f"No access_token in response: {data}")
    return token


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{BASE_URL}/admin/api/{API_VERSION}/graphql.json"
    import time
    status, body = 0, b""
    for attempt in range(4):
        status, body = _http(url, payload, headers, "POST", 60)
        if status == 429 or status >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    text = body.decode(errors="replace")
    if status != 200:
        raise FeedError(f"GraphQL request failed ({status}): {text[:1500]}")
    result = json.loads(text)
    if result.get("errors"):
        raise FeedError(f"GraphQL errors: {json.dumps(result['errors'], ensure_ascii=False)[:2000]}")
    return result.get("data") or {}


def fetch_products(token: str) -> list[dict]:
    products = []
    after = None
    while True:
        data = graphql(token, PRODUCTS_QUERY, {"first": 100, "after": after})
        conn = data["products"]
        products.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return products


# ── helpers ─────────────────────────────────────────────────────────────

def strip_html(raw_html: str | None) -> str:
    """Plain text from descriptionHtml: tags stripped, entities unescaped,
    whitespace collapsed, capped at 5000 chars."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:5000]


def numeric_id(gid: str) -> str:
    return (gid or "").rsplit("/", 1)[-1]


def to_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── feed building ───────────────────────────────────────────────────────

def add_item(channel: ET.Element, product: dict) -> bool:
    variants = (product.get("variants") or {}).get("nodes") or []
    variant = variants[0] if variants else {}
    sku = (variant.get("sku") or "").strip()
    if not sku:
        return False  # g:id is mandatory - skip products with no SKU

    price_f = to_float(variant.get("price"))
    compare_f = to_float(variant.get("compareAtPrice"))
    inv_qty = variant.get("inventoryQuantity")
    availability = "in_stock" if (inv_qty or 0) > 0 else "out_of_stock"

    images = [n["url"] for n in (product.get("images") or {}).get("nodes") or [] if n.get("url")]
    image_link = images[0] if images else None
    additional_images = images[1:6]

    tags = product.get("tags") or []
    category_handle = next((t.split(":", 1)[1] for t in tags if t.startswith("category:")), None)
    category_title = CATEGORY_MAP.get(category_handle, category_handle or "")
    product_type_feed = f"Pharmacy > {category_title}" if category_title else "Pharmacy"
    kept_tags = [t for t in tags if t not in TECHNICAL_TAGS and not t.startswith("category:")]

    refill = {m["key"]: m["value"] for m in (product.get("metafields") or {}).get("nodes") or []}
    units_per_pack = refill.get("units_per_pack")
    form = refill.get("form")

    pil_url = ((product.get("pil") or {}).get("reference") or {}).get("url")

    item = ET.SubElement(channel, "item")

    def sub(tag: str, text) -> None:
        if text is None or text == "":
            return
        el = ET.SubElement(item, tag)
        el.text = str(text)

    sub("g:id", sku)
    sub("title", product.get("title"))
    sub("description", strip_html(product.get("descriptionHtml")))
    sub("link", f"{BASE_URL}/products/{product.get('handle')}")
    sub("g:image_link", image_link)
    for img in additional_images:
        sub("g:additional_image_link", img)

    # The storefront displays EUR at a fixed 1.0 conversion rate, so the raw
    # Shopify price number already IS the EUR amount - currency hard-coded.
    if compare_f is not None and price_f is not None and compare_f > price_f:
        sub("g:price", f"{compare_f:.2f} EUR")
        sub("g:sale_price", f"{price_f:.2f} EUR")
    elif price_f is not None:
        sub("g:price", f"{price_f:.2f} EUR")

    sub("g:availability", availability)
    sub("g:brand", product.get("vendor"))
    sub("g:condition", "new")
    sub("g:product_type", product_type_feed)

    sub("epace:shopify_product_id", numeric_id(product.get("id")))
    sub("epace:category", category_handle or "")
    sub("epace:product_kind", product.get("productType"))
    sub("epace:units_per_pack", units_per_pack)
    sub("epace:form", form)
    sub("epace:pil_url", pil_url)
    sub("epace:stock", inv_qty if inv_qty is not None else 0)
    sub("epace:tags", ",".join(kept_tags))

    return True


def build_feed(products: list[dict]) -> tuple[ET.Element, int]:
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:g": "http://base.google.com/ns/1.0",
        "xmlns:epace": "https://epace.cz/ns/pharmacy-feed/1.0",
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "ePACE Pharmacy - product feed (hackathon demo)"
    ET.SubElement(channel, "link").text = BASE_URL
    ET.SubElement(channel, "description").text = (
        "Product feed for the ePACE Pharmacy store, a mock Shopify storefront built for "
        "the Bloomreach AI Hackathon 2026. This is a demo catalog, not a real pharmacy."
    )
    ET.SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    count = 0
    for product in products:
        if add_item(channel, product):
            count += 1
    return rss, count


def main() -> None:
    token = get_access_token()
    products = fetch_products(token)
    rss, count = build_feed(products)

    ET.indent(rss, space="  ")
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8") + b"\n"

    # Validate before writing anything to disk.
    ET.fromstring(xml_bytes)

    with open("feed.xml", "wb") as f:
        f.write(xml_bytes)

    print(f"Wrote feed.xml: {count} items from {len(products)} products fetched "
          f"(shop={SHOP_DOMAIN}, api={API_VERSION}).", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except FeedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
