# Xponent — Multi-Tenant Live Aggregation Layer

Fast, fresh, exact tenant dashboards over a changing OLTP database — without
overloading it. CDC-driven incremental Z-set rollups in an isolated summary
store, queried with bucket-bounded reads.

> **Build status:** foundation only. Databases + schemas + synthetic data
> tooling are in place (tasks T1, T2). The CDC worker and query API land next
> (T4, T7). See `docs/PROGRESS.md` for live task state and `docs/IMPLEMENTATION_PLAN.md`
> for the full roadmap. No benchmark numbers are claimed until a manifested run
> produces them.

## What exists now

- `db/primary/` — OLTP schema (tenants, products, customers, orders, order_items),
  indexes, least-privilege replication role, logical-replication publication.
  `orders`/`order_items` use `REPLICA IDENTITY FULL` for Z-set retractions.
- `db/summary/` — rollup schema (daily / hourly / **minutely** order buckets,
  daily product sums, digests, checkpoint), indexes, and tenant-isolating RLS
  with a non-superuser `agg_reader` role for the API.
- `docker-compose.yml` — pg-primary, pg-summary, redis, each with CPU/memory
  caps and auto-applied schema. Worker/API services are commented until their code lands.
- `scripts/` — deterministic synthetic data generator + mutation stream
  (`seed_dataset.py`, `gen_mutations.py`, `apply_mutations.py`).

## Quick start (foundation)

```bash
cp .env.example .env
docker compose up -d          # or: make up
docker compose ps             # all three healthy
make verify-caps              # confirm CPU/memory limits are enforced
```

Apply-on-boot runs `db/primary/*.sql` and `db/summary/*.sql` in order. Inspect:

```bash
make psql-summary -%  # \dt to list rollup tables
```

## Seed synthetic data (no organizer dataset — we own the oracle)

Install the script deps first (one-time):

```bash
pip install -r scripts/requirements.txt
```

Then seed a tier (volumes, **not** performance results — decision D5):

```bash
make seed-dev     # 50k / 5 tenants   — fast inner loop
make seed-demo    # 1M / 10 tenants   — demo default
make seed-bench   # 10M / 100 tenants — headline benchmark
```

The generator is deterministic given `--seed`; every run writes a manifest so
results are reproducible. Because we generate the data, a direct SQL query over
the base tables (at a quiesced snapshot) is the authoritative correctness oracle.

## Design decisions (approved)

See `docs/OPEN_DECISIONS.md`. In brief: PostgreSQL only; Python end-to-end;
non-aligned ranges answered via a minute-grain summary bucket (never the primary);
gross-default sales semantics (net carried); no JWT (path + dev header + RLS);
seed-quiesced-then-stream bootstrap.

## Endpoints (coming in T7)

`GET /api/v1/tenants/{id}/orders/summary` (FR-1), `/orders/trends` (FR-2),
`/products/top` (FR-3), `/orders/by-status` (FR-4), plus `/health`, `/freshness`,
and admin verify/rebuild (B-2).
