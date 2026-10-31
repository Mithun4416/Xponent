# Xponent — Multi-Tenant Live Aggregation Layer

Fast, fresh, exact tenant dashboards over a changing OLTP database — **without
overloading it**. Each tenant (a restaurant, in the demo) has its **own source
database**; Xponent captures their changes with CDC, keeps **incremental rollups**
in a separate **summary store**, and serves dashboard queries from those rollups
so the query cost depends on the number of time-buckets, not the number of rows.

- **Fast** — dashboard reads hit pre-aggregated buckets (sub-second, usually sub-100ms).
- **Fresh** — Change-Data-Capture streams new orders into the rollups within seconds.
- **Safe for the source DB** — dashboards never scan the OLTP tables.
- **Separate tenants** — path identity + PostgreSQL Row-Level Security; zero cross-tenant leakage.
- **Correct** — integer-cents math, exact for arbitrary (non-aligned) time ranges.
- **Scales** — tested design for 10M orders / 100 tenants.

---

## 1. Quick start (judges: start here)

**Prerequisites:** Docker (Compose v2) running, Python 3.10+, and the script deps.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r scripts/requirements.txt
./scripts/quickstart.sh
```

`quickstart.sh` does everything, printing each step:

1. starts the two PostgreSQL databases + Redis,
2. provisions the per-tenant source databases (`tenant_1`, `tenant_2`),
3. seeds ~200k synthetic orders across them,
4. bootstraps the rollups and creates one CDC slot per tenant,
5. starts the CDC worker and the query API,
6. waits for health, then prints the URL.

Then open **http://localhost:8000** and enter the operator key **`xpo-admin-dev`**
in the sidebar (dev default).

> Bigger dataset: `ROWS=1000000 ./scripts/quickstart.sh`.
> For the PRD headline (10M / 100 tenants) see **§6 Scale**.

### What you can do in the dashboard

- **Organization view** (platform owner): platform-wide KPIs, a **restaurant
  leaderboard** (click any row to drop into that restaurant's own dashboard),
  an **Ask across all restaurants** box, **Live CDC activity**, and a
  **Databases** browser that shows every source DB and the summary store.
- **Restaurant view** (seller): that restaurant's own numbers only (server-enforced),
  a plain-English **Ask** box (instant), and an **Orders** tab to create / edit /
  cancel / delete orders and watch the numbers update live.
- **Live load:** `make load` streams **500 orders/sec** into the source DBs so you
  can watch the rollups keep up while freshness stays green.

---

## 2. The two databases (and how to prove they're both working)

This is the core of the design — **two separate databases**:

| | **Source / OLTP DB(s)** | **Summary / rollup store** |
|---|---|---|
| Container | `xpo-pg-primary` (`:5432`) | `xpo-pg-summary` (`:5433`) |
| Databases | `tenant_1`, `tenant_2` (one per tenant) | `agg_db` |
| Holds | raw `orders`, `order_items`, `products`, `customers` | pre-computed `agg_*` rollups + digests |
| Written by | the app / load generator | the **CDC worker** only |
| Read by the dashboard | **never** (except tiny sub-minute edges) | **always** |

**Verify the fast-retrieval path (rollups) yourself:**

```bash
# 1. The summary store has pre-computed rollups (fast path):
docker compose exec pg-summary psql -U agg_user -d agg_db \
  -c "SELECT tenant_id, day_bucket, status, order_count, gross_sales_cents
      FROM agg_orders_daily ORDER BY day_bucket DESC LIMIT 10;"

# 2. The dashboard number matches a DIRECT query on the raw source table
#    (correctness) — pick a day and compare:
docker compose exec pg-primary psql -U postgres -d tenant_1 \
  -c "SELECT count(*), sum(total_amount_cents) FROM orders
      WHERE created_at::date = '2026-07-02';"
docker compose exec pg-summary psql -U agg_user -d agg_db \
  -c "SELECT sum(order_count), sum(gross_sales_cents) FROM agg_orders_daily
      WHERE tenant_id=1 AND day_bucket='2026-07-02';"
# the two should match exactly.
```

**Verify freshness / rollups update live:** in the **Orders** tab create an order,
then re-run your question in **Ask** — the count goes up within seconds. Or watch
**Organization → Live activity** show the CDC events flowing in.

**Verify Receipts (the self-healing differentiator):** open **Diagnostics** (gear
icon) → **Verify** proves every `(tenant, day)` bucket's digest matches the source;
**Repair day** re-derives one bucket reading <1% of rows. Or:

```bash
curl -s "http://localhost:8000/api/v1/admin/verification?tenant_id=1&run=true&sample=200" \
  -H "X-Admin-Key: xpo-admin-dev" | python3 -m json.tool
```

Automated checks for both (no DB needed):

```bash
python3 tests/correctness/test_receipts_core.py      # digest + targeted repair
python3 tests/correctness/test_range_decomposer.py   # exact non-aligned ranges
```

---

## 3. Where exactly is the data? (rows and columns)

Everything is in PostgreSQL. Browse it two ways:

**A) In the dashboard:** Organization view → **Databases**. Pick a database
(each tenant's source DB, or the summary store), pick a table, and you see the
real columns and rows, with an estimated row count.

**B) With `psql`:**

```bash
# Tenant 1's own source database — raw transactional rows:
docker compose exec pg-primary psql -U postgres -d tenant_1 -c "\dt"          # list tables
docker compose exec pg-primary psql -U postgres -d tenant_1 -c "\d orders"    # columns of orders
docker compose exec pg-primary psql -U postgres -d tenant_1 -c "SELECT * FROM orders LIMIT 5;"

# Tenant 2 is a SEPARATE database on the same cluster:
docker compose exec pg-primary psql -U postgres -d tenant_2 -c "SELECT count(*) FROM orders;"

# The summary store — pre-computed rollups the dashboard reads:
docker compose exec pg-summary psql -U agg_user -d agg_db -c "\dt"
docker compose exec pg-summary psql -U agg_user -d agg_db -c "\d agg_orders_daily"
```

Key tables — **source** (`tenant_N`): `orders(order_id, tenant_id, customer_id,
status, total_amount_cents, created_at, …)`, `order_items`, `products`,
`customers`, `tenants`. **Summary** (`agg_db`): `agg_orders_daily / _hourly /
_minutely(tenant_id, bucket, status, order_count, gross_sales_cents,
net_sales_cents)`, `agg_products_daily`, `bucket_digests` (Receipts),
`cdc_checkpoint` (freshness), `cdc_activity` (live feed).

---

## 4. API endpoints (FR mapping)

Seller endpoints are scoped by the `X-Dev-Tenant-ID` header (must equal the path tenant):

| FR | Endpoint |
|----|----------|
| FR-1 counts & totals | `GET /api/v1/tenants/{id}/orders/summary?from=&to=` |
| FR-2 trend over time | `GET /api/v1/tenants/{id}/orders/trends?from=&to=&granularity=day\|hour` |
| FR-3 top-N | `GET /api/v1/tenants/{id}/products/top?from=&to=&limit=` |
| FR-4 breakdown | `GET /api/v1/tenants/{id}/orders/by-status?from=&to=` |
| FR-5 tenant filter | enforced on every call (path + RLS; cross-tenant → 403) |
| FR-6 live updates | `POST/PATCH/DELETE /api/v1/tenants/{id}/orders…` → CDC → rollups |
| FR-7 simple API | this table + `GET /api/v1/health` |
| NLQ | `POST /api/v1/nlq` (plain-English → validated plan → rollup read) |
| Org | `GET /api/v1/org/overview`, `/org/activity` (operator key) |
| Explorer | `GET /api/v1/explore/databases`, `/explore/rows` (operator key) |

Bonus features: **B-1** freshness watermark on every response · **B-2** Receipts
verify + targeted repair (`/api/v1/admin/verification`, `/admin/rebuild`) ·
**B-3** per-tenant query rate limit (429 past `TENANT_MAX_CONCURRENCY`) ·
**B-4** metrics defined in [`app/metrics.json`](app/metrics.json), not code ·
**B-5** monitoring (`/api/v1/status/pipeline` + the dashboard's Data status).

To add a metric with **no code change** (B-4): edit `app/metrics.json`, add e.g.
`{"name":"gross_per_order","label":"Gross per order","expr":"gross / orders","format":"currency"}`,
and it appears in the summary response and the dashboard's Quick insights.

---

## 5. Live load generator (handles data efficiently, not one row at a time)

```bash
make load                      # 500 orders/sec for 60s into the source DBs
RATE=1000 DURATION=120 make load
```

Each inserted order flows through CDC into the rollups. Watch **Organization →
Live activity** and the KPIs climb while **Data status** stays "Up to date".

---

## 6. Scale — is the dataset big enough?

The quickstart seeds a small set (~200k) so judges start in under a minute. The
design is built for the PRD's main run (**10M orders / 100 tenants**): dashboard
query cost depends on the number of time-buckets, not rows, so it stays sub-second
as data grows. To run at the headline scale on the shared-DB topology:

```bash
make seed-bench     # 10M / 100 tenants (minutes to seed)
```

For sustained growth during judging, leave `make load` running — it adds hundreds
of orders/sec indefinitely.

---

## 7. Design decisions & docs

- `docs/PRD_COMPLIANCE.md` — every goal / FR / bonus / target, mapped to code + how to verify.
- `docs/IMPLEMENTATION_PLAN.md`, `docs/TECHNICAL_GAP_ANALYSIS.md`, `docs/OPEN_DECISIONS.md`, `docs/PROGRESS.md`.
- In brief: PostgreSQL only; Python end-to-end; CDC (wal2json) → incremental Z-set
  rollups; non-aligned ranges answered via day/hour/minute buckets (never the source);
  tenant isolation via path + RLS; no JWT (PRD excludes auth).

## 8. Troubleshooting

- **Org view empty / 403** — enter the operator key `xpo-admin-dev` in the sidebar.
- **`psycopg` missing** — `pip install -r scripts/requirements.txt` in your venv.
- **Ask is slow** — the first LLM call can be slow; recognised questions use the
  instant rules path (no LLM). Set `NLQ_PROVIDER=rules` to force instant always.
- **Reset everything** — `docker compose down -v && ./scripts/quickstart.sh`.
