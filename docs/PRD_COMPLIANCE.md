# PRD Compliance — Xponent

Audit of the implementation against [`01_PRD.docx.md`](../01_PRD.docx.md). Status
is one of **DONE** (implemented), **DONE*** (implemented; headline *number* needs a
manifested benchmark run — we do not fabricate results), or **PARTIAL**.

Last updated: 2026-11-01.

## Goals (§2)

| # | Goal | Status | Where / how to verify |
|---|------|--------|-----------------------|
| 1 | **Fast** (<1s dashboard queries) | DONE* | Reads hit `agg_*` rollups, not raw rows. Each response carries `execution_ms`; the dashboard shows query time. Headline p95 at 10M → `benchmarks/` run. |
| 2 | **Fresh** (minutes, faster better) | DONE | CDC worker streams WAL → rollups in seconds. Every response has a freshness watermark (B-1). See Live activity feed. |
| 3 | **Safe for the source DB** | DONE | Dashboards read only the summary store; the source DB is touched only for tiny sub-minute edges. CPU/mem caps in `docker-compose.yml`; `make verify-caps`. |
| 4 | **Separate tenants** | DONE | Per-tenant source DBs + path identity (`get_tenant_context`, 403 on mismatch) + PostgreSQL RLS (`FORCE ROW LEVEL SECURITY`, non-owner `agg_reader`). Tests in `tests/`. |
| 5 | **Correct** (matches direct SQL) | DONE | Integer-cents; exact day/hour/minute decomposition for non-aligned ranges. Compare rollup vs direct `SELECT` (README §2). `test_range_decomposer.py`. |
| 6 | **Scales** (10× growth) | DONE* | Query cost ∝ bucket count, not rows. `make seed-bench` (10M/100). 1M→10M ratio from a benchmark run. |

## Functional requirements (§3, must-have)

| ID | Feature | Status | Endpoint / evidence |
|----|---------|--------|---------------------|
| FR-1 | Counts & totals for a range | DONE | `GET /tenants/{id}/orders/summary` |
| FR-2 | Trend over time (hour/day) | DONE | `GET /tenants/{id}/orders/trends` |
| FR-3 | Top-N list | DONE | `GET /tenants/{id}/products/top` |
| FR-4 | Breakdown by category (status) | DONE | `GET /tenants/{id}/orders/by-status` |
| FR-5 | Tenant filter on every query | DONE | path + RLS; cross-tenant → 403; org endpoints need operator key |
| FR-6 | New data shows up, incl. updates | DONE | Orders CRUD → source DB → CDC → rollups; Live activity feed; create-then-re-ask demo |
| FR-7 | Simple REST API + README | DONE | README §4 endpoint table + `/api/v1/health` |

## Bonus (§3) — all five implemented (PRD asks for any)

| ID | Feature | Status | Evidence |
|----|---------|--------|----------|
| B-1 | Freshness ("updated N ago") | DONE | `freshness_block` on every response; dashboard Data status |
| B-2 | Rebuild / repair on corruption | DONE | Receipts: `/admin/verification`, `/admin/rebuild` (targeted, <1% rows); Diagnostics panel |
| B-3 | Limit heavy queries per tenant | DONE | `app/core/limits.py` — 429 past `TENANT_MAX_CONCURRENCY`; shown in `/status/pipeline` |
| B-4 | New metric by editing a config file | DONE | `app/metrics.json` + `app/services/metrics.py` (safe evaluator); appears in summary `derived` |
| B-5 | Monitoring page (query time, lag) | DONE | `/api/v1/status/pipeline` + dashboard (query time, freshness, live CDC activity) |

## Performance targets (§4)

Hardware/image/seed manifests accompany every number; **no number is claimed
until a manifested run produces it** (test-plan discipline).

| Measure | Good | Great | How to produce the number |
|---------|------|-------|---------------------------|
| Query speed @10M | <1s | <200ms | `benchmarks/` latency harness per tenant class |
| Freshness | <5min | <30s | `apply_mutations.py` + watermark vs external clock |
| DB safety | <25% | <10% | pgbench baseline vs under dashboard+CDC load; `make verify-caps` first |
| Correctness | most | all | rollup vs direct SQL on a quiesced snapshot (README §2) |
| Growth 1M→10M | <5× | <2× | same latency suite at both scales |

## "Must run easily" (§4)

**DONE** — `./scripts/quickstart.sh` brings the whole system up from a clean clone
and prints the dashboard URL; Docker Compose throughout. See README §1.

## Honest gaps

- Headline latency/throughput/DB-safety **numbers** require a benchmark run on the
  target hardware (the harness exists; the numbers are not fabricated here).
- Top-N (FR-3) is exact for day-aligned windows (the PRD "this week" case);
  sub-hour-exact Top-N is deferred (documented in `TECHNICAL_GAP_ANALYSIS.md`).
- Quickstart seeds a small set for speed; use `make seed-bench` for the 10M run.
