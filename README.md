# ePACE Pharmacy product feed

A daily-refreshed, publicly hosted XML product feed for the **ePACE
Pharmacy** mock Shopify store (`team-epace.myshopify.com`, ~97 products
tagged `pharmacy`), built for ePACE's **Bloomreach AI Hackathon 2026**
submission.

The dev storefront is password-protected and Shopify's file CDN caches an
uploaded file's URL for a year, so there is no way to host a stable,
daily-updated feed URL directly on Shopify. This repo (public, on GitHub) is
the workaround: a GitHub Action re-generates `feed.xml` from the Shopify
Admin API every day and commits it, so the raw file URL below always serves
the latest data.

**Public feed URL (always current):**

```
https://raw.githubusercontent.com/domid-epace/epace-pharmacy-feed/main/feed.xml
```

## What's in the feed

RSS 2.0 with the Google Shopping namespace (`g:`) plus a custom `epace:`
namespace for extra pharmacy-specific fields. One `<item>` per product
(variant-1 SKU), sorted by SKU so daily diffs stay small.

| Field | Source | Notes |
|---|---|---|
| `g:id` | variant SKU | required, used as the item identifier |
| `title` | product title | |
| `description` | `descriptionHtml` | HTML stripped, whitespace collapsed |
| `link` | `https://team-epace.myshopify.com/products/<handle>` | |
| `g:image_link` / `g:additional_image_link` | product media | up to 5 images total |
| `g:price` / `g:sale_price` | variant `price` / `compareAtPrice` | see currency note below |
| `g:availability` | variant `inventoryQuantity` | `in_stock` / `out_of_stock` |
| `g:brand` | `vendor` | |
| `g:condition` | fixed | always `new` |
| `g:product_type` | collections | `"Pharmacy > <category title>"`, category = the collection that isn't the blanket `pharmacy` one |
| `g:identifier_exists` | fixed | always `no` (no GTIN/MPN/brand-ID data in this mock catalogue) |
| `epace:shopify_product_id` | numeric Shopify product ID | |
| `epace:category` | category collection handle | |
| `epace:units_per_pack` | metafield `refill.units_per_pack` | when set |
| `epace:form` | metafield `refill.form` | when set |
| `epace:pil_url` | metafield `pharmacy.pil` (file reference) | patient information leaflet URL, when present |
| `epace:stock` | variant `inventoryQuantity` | raw number |
| `epace:tags` | product tags | comma-separated |

**Currency note:** the Shopify Admin API always returns money amounts in the
shop's base currency (GBP for `team-epace`), but the published storefront's
presentation currency is fixed to EUR at a 1.0 conversion rate (a deliberate
hackathon-demo setting). So the script takes the raw Admin API numeric
amount and labels it `EUR` directly, with no conversion math - see the
`CURRENCY_CODE` comment in `generate_feed.py`.

## Files

- `generate_feed.py` - stdlib-only Python 3.11 script. Gets a Shopify Admin
  API token via the OAuth2 `client_credentials` grant, paginates
  `products(query: "tag:pharmacy status:active")` over GraphQL, and writes
  `feed.xml`. It only rewrites the file when actual item content changed
  (ignoring `lastBuildDate`), so the daily Action doesn't create commit
  noise on days nothing in the catalogue changed.
- `.github/workflows/feed.yml` - runs the script daily and pushes `feed.xml`
  if it changed, plus `workflow_dispatch` for manual runs.
- `feed.xml` - the generated feed itself.

## Schedule

The GitHub Action runs on cron `0 7 * * *` (07:00 UTC), which is 09:00
Europe/Prague during CEST (summer time, through 2026-10-25). Cron is fixed
UTC and doesn't follow DST, so after the autumn 2026 clock change 07:00 UTC
becomes 08:00 local time - see the comment in `feed.yml`. It can also be run
on demand from the Actions tab (`workflow_dispatch`).

## One-time setup

Add repository secrets **`SHOPIFY_CLIENT_ID`** and **`SHOPIFY_CLIENT_SECRET`**
(Settings > Secrets and variables > Actions). Values are in the team's
`epace-shopify-mcp/.env`. Without them the Action fails fast with a clear
error instead of running.
