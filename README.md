# ePACE Pharmacy — product feed

A daily-refreshed, publicly hosted XML product feed for the **ePACE Pharmacy**
mock Shopify store (`team-epace.myshopify.com`), built for the **Bloomreach
AI Hackathon 2026**.

The dev storefront is password-protected and Shopify's file/CDN URLs are
cached for a year, so the feed cannot be hosted on Shopify itself. Instead,
this repo pulls the catalog from the Shopify Admin GraphQL API and commits a
static `feed.xml`, served over GitHub's raw content CDN.

**Feed URL (stable):**
`https://raw.githubusercontent.com/domid-epace/epace-pharmacy-feed/main/feed.xml`

## Schedule

A GitHub Action (`.github/workflows/feed.yml`) rebuilds and commits
`feed.xml` every day at **07:00 UTC (09:00 Prague time / CEST)**, and can
also be run on demand from the Actions tab (`workflow_dispatch`). After the
winter DST switch (last Sunday of October) the same cron fires at 08:00
Prague time, until DST resumes in spring.

## Format

RSS 2.0, Google Shopping style, `<item>` per product:

- `xmlns:g="http://base.google.com/ns/1.0"` — standard Google Shopping fields
- `xmlns:epace="https://epace.cz/ns/pharmacy-feed/1.0"` — custom ePACE fields

Channel: title, link, description (notes this is a mock/demo store),
`lastBuildDate`.

| Field | Source |
|---|---|
| `g:id` | Variant SKU |
| `title` | Product title |
| `description` | Plain text from `descriptionHtml` (tags stripped, whitespace collapsed, max 5000 chars) |
| `link` | `https://team-epace.myshopify.com/products/<handle>` |
| `g:image_link` | Featured/first product image |
| `g:additional_image_link` | Up to 5 more images |
| `g:price` / `g:sale_price` | Variant price, hard-coded `EUR` (storefront shows EUR at a fixed 1:1 rate). `g:sale_price` + `g:price` = compare-at price only when `compareAtPrice > price` |
| `g:availability` | `in_stock` / `out_of_stock` from inventory quantity |
| `g:brand` | Vendor |
| `g:condition` | Always `new` |
| `g:product_type` | `Pharmacy > <category>`, from the `category:<handle>` tag |
| `epace:shopify_product_id` | Numeric Shopify product ID |
| `epace:category` | Category handle (e.g. `pain-fever`) |
| `epace:product_kind` | Shopify `productType` |
| `epace:units_per_pack` | Metafield `refill.units_per_pack` |
| `epace:form` | Metafield `refill.form` |
| `epace:pil_url` | URL of the PIL file in metafield `pharmacy.pil` |
| `epace:stock` | Inventory quantity |
| `epace:tags` | Remaining product tags, comma-joined (excludes `pharmacy`, `category:*`, `has-pil`) |

Only products tagged `pharmacy` with `status: active` are included.

## One-time setup (do this before the schedule can run)

1. Go to **Settings → Secrets and variables → Actions** on this repo and add
   two repository secrets:
   - `SHOPIFY_CLIENT_ID`
   - `SHOPIFY_CLIENT_SECRET`

   Values are in the hackathon folder `epace-shopify-mcp/.env`.
2. Go to the **Actions** tab, open **Rebuild product feed**, and run it once
   manually (`Run workflow`) to confirm the secrets work and produce a fresh
   commit.

After that, the daily schedule takes over — no further action needed.

## Files

- `generate_feed.py` — Python 3.11, stdlib only. Authenticates via OAuth2
  `client_credentials` against Shopify Admin, paginates
  `products(query:"tag:pharmacy status:active")` over Admin GraphQL, writes
  `feed.xml`.
- `.github/workflows/feed.yml` — daily schedule + manual trigger.
- `feed.xml` — the generated feed (committed by the workflow; also seeded
  once manually so the URL works immediately).
