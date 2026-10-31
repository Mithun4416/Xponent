I now have every precise value I need. Here is the complete QA, correctness, and performance benchmarking plan.

---

# Testing, Validation and Performance Benchmark Plan
## Multi-Tenant Live Aggregation Layer

**Document status:** This is a *planned* test strategy. Every procedure below is written to be executed and re-executed. **No benchmark results are reported here** — all "Actual Result" cells are left as placeholders to be filled only after a real run. Planned tests and executed tests are kept strictly separate (see §13).

**Role perspective:** Authored from the combined viewpoint of Principal QA, Performance Engineering, Database Reliability Engineering, and Distributed Systems Testing.

A note on scope and honesty: the system design claims aggressive numbers (p95 < 200 ms, lag 150 ms–1.5 s, <4% OLTP overhead). This plan treats all of those as *hypotheses to be falsified*, not facts. The organizer's PRD "Good"/"Great" thresholds are the only binding acceptance bar; the design's self-stated targets are recorded as internal stretch goals and are explicitly marked as unverified.

---

## 1. Testing Objectives

The central claim under test is: *the system answers tenant dashboard queries quickly and exactly, stays fresh within seconds, and does this without materially slowing the primary OLTP database.* Each objective below decomposes that claim into a falsifiable property tied to a specific PRD requirement.

**Functional correctness.** Every must-have endpoint (EP-03 summary, EP-04 trends, EP-05 top-N, EP-06 by-status) plus the composite dashboard (EP-02), health (EP-01), freshness (EP-07), verification (EP-08), and rebuild (EP-09) behaves per its contract: correct status codes, correct error envelope, correct parameter validation, and schema-valid JSON. This maps to SRS FR-006 through FR-023 and PRD FR-1…FR-7.

**Aggregation accuracy.** Numbers returned by the API are bit-for-bit identical to a direct SQL aggregation evaluated at the same LSN/snapshot, including arbitrary non-aligned `from`/`to` boundaries and post-cancellation states. This is the single most heavily weighted correctness objective (PRD "matches on all queries"). Because the system uses integer-cents arithmetic and query-time Top-N ranking, exactness — not approximation — is the bar.

**Performance.** API latency percentiles (p50/p95/p99) per endpoint at 10M rows, aggregation throughput (events/sec sustained by the CDC+coalescer path), and CDC processing lag. PRD query target: <1 s (Good), <200 ms (Great).

**Scalability.** Latency degradation ratio from 1M→10M records must stay <5× (Good) / <2× (Great). 5M is included as an interpolation point to detect non-linear inflection before the 10M run.

**Data freshness.** Commit-to-visibility lag for both inserts and `placed→cancelled` updates: <5 min (Good), <30 s (Great). The `FRESH`/`STALE_WARNING` boundary is hard-coded at 30,000 ms, so self-reported `lag_ms` is also validated against externally measured lag.

**Tenant isolation.** Zero cross-tenant leakage under any parameter, token, error-message, or timing combination (PRD FR-5, non-negotiable). Includes RLS defense-in-depth verification.

**Reliability.** Exactly-once aggregate effects across CDC crash/replay, duplicate delivery, out-of-order-within-constraints events, primary restart, summary-store failure, and targeted/full rebuild — verified by digest reconciliation against ground truth.

---

## 2. Testing Environment

**Hardware specifications (reference laptop).** Target is a single developer laptop, since CON-001 forbids cloud clusters. Record the actual machine before each run; results are only comparable within the same hardware profile. Capture: CPU model and physical/logical core count, total RAM, storage type (NVMe SSD strongly recommended — spinning disk invalidates latency targets), free disk (DEP-002 requires ~15–25 GB free), OS and kernel version, and whether the machine is on AC power with thermal throttling disabled for the run. Thermal throttling on a laptop is a real confound for sustained 10M benchmarks and must be logged.

**Software versions.** PostgreSQL 16 (primary, `wal_level=logical`, `max_slot_wal_keep_size=2048MB`, `REPLICA IDENTITY FULL` on `orders` and `order_items`); PostgreSQL 16 (summary store, separate instance); Redis 7 Alpine; Python 3.12 with `psycopg3`/`asyncpg`/`xxhash`; FastAPI + Uvicorn; DuckDB with `postgres_scanner` as the correctness oracle; Docker + Docker Compose. Pin every image digest and every Python package version in a committed lockfile so the harness is reproducible.

**Container resource envelope (must match the design, since the DB-safety test depends on it).** pg-primary `cpus: 2.0 / 4096M`; pg-summary `cpus: 1.0 / 2048M`; cdc-worker `cpus: 1.0 / 1024M`; agg-api `cpus: 1.0 / 1024M`; redis-cache `cpus: 0.5 / 256M`. These caps are themselves part of the system-under-test: the "less than 10% OLTP slowdown" claim rests on the summary/worker/API containers being prevented from stealing the primary's reserved cores. Verify the caps are actually applied (`docker inspect`) before trusting any safety result.

**Dataset characteristics.** Three tiers at 1M / 5M / 10M order rows. Tenant count 10 at 1M, 100 at 5M and 10M. Realistic power-law (Pareto) skew: a handful of "whale" tenants hold 1M–3M orders each while the median tenant holds ~20k–50k. Status domain `{placed, shipped, cancelled}`. Currency as integer cents. Timestamps spread across a multi-month window in UTC, with a controllable fraction of historical updates and cancellations.

**Tenant distribution.** The skew is a first-class test variable, not incidental. Every latency and freshness test must be reported separately for (a) the largest whale tenant, (b) a median tenant, and (c) a smallest tenant, because the core architectural claim is that query cost is bucket-bound, not row-bound. A single aggregate percentile across tenants would hide exactly the skew the PRD cares about.

**Workload assumptions.** Baseline OLTP is a mixed read/write order-placement workload (inserts, status updates, point reads). Dashboard workload is concurrent clients hitting the four must-have endpoints with randomized tenants and randomized `from`/`to` windows. CDC is active and streaming in all realistic tests except the explicit "baseline without solution" control.

---

## 3. Test Dataset Generation

The generator must be deterministic given a seed so any mismatch is reproducible. Note an open dependency (DEP-001, AMB-001/AMB-004): the *organizer's* generator is authoritative for schema and distribution. This synthetic generator is for pre-organizer dry runs and must be swappable for the official one. Confirm at kickoff whether orders reference a child `order_items` table (AMB-004) and whether sales exclude cancelled orders (AMB-002) — both change the ground-truth queries.

Design goals for the generator: configurable total row count (1M/5M/10M), configurable tenant count, Pareto skew across tenants, a configurable UTC time span, a configurable product catalog size per tenant, and a configurable *mutation stream* (a post-load sequence of INSERTs, status UPDATEs including `placed→cancelled`, and amount changes) emitted with recorded commit timestamps so freshness and retraction tests have ground truth.

A reference generator skeleton:

```python
# tools/gen_dataset.py  — deterministic synthetic loader
import argparse, random, hashlib
from datetime import datetime, timedelta, timezone
import psycopg

def pareto_weights(n_tenants, alpha=1.16):
    # alpha ~1.16 gives a strong 80/20-style skew
    w = [ (i+1) ** (-alpha) for i in range(n_tenants) ]
    s = sum(w)
    return [x/s for x in w]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, required=True)          # 1_000_000 / 5_000_000 / 10_000_000
    ap.add_argument("--tenants", type=int, required=True)        # 10 / 100
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--span-days", type=int, default=120)
    ap.add_argument("--products-per-tenant", type=int, default=1000)
    ap.add_argument("--cancel-frac", type=float, default=0.06)   # share eventually cancelled
    ap.add_argument("--dsn", required=True)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    weights = pareto_weights(a.tenants)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    statuses = ["placed", "shipped", "cancelled"]

    with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
        # tenants / products / customers seeded first (omitted for brevity) ...
        # bulk COPY orders for speed; do NOT use row-by-row INSERT at 10M scale
        with cur.copy("COPY orders (order_id, tenant_id, customer_id, status, "
                      "total_amount_cents, created_at, updated_at) FROM STDIN") as cp:
            for oid in range(1, a.rows + 1):
                t = rng.choices(range(1, a.tenants + 1), weights=weights)[0]
                sec = rng.randint(0, a.span_days * 86400)
                created = start + timedelta(seconds=sec)
                # weight toward non-cancelled at load time; cancellations come via mutation stream
                st = "cancelled" if rng.random() < a.cancel_frac * 0.3 else rng.choice(["placed","shipped"])
                amt = rng.randint(500, 50000)
                cust = rng.randint(1, max(2, a.rows // 20))
                cp.write_row((oid, t, cust, st, amt, created, created))
        conn.commit()
    print(f"Loaded {a.rows} orders across {a.tenants} tenants (seed={a.seed})")

if __name__ == "__main__":
    main()
```

The *mutation stream* for freshness/retraction tests is generated separately and written to a replayable file (one line per operation, each carrying a client-side commit timestamp placeholder filled at apply time):

```python
# tools/gen_mutations.py — emits a replayable update/cancel stream with recorded wall-clock
# format: op<TAB>order_id<TAB>new_status<TAB>new_amount_cents
# applied later by apply_mutations.py which records commit_ts = clock_timestamp() per row
```

Reproducibility rule: the exact `--seed`, row count, tenant count, span, and generator git SHA are logged into the results manifest for every run. Two runs with the same manifest must produce identical base tables (verify with `SELECT md5(string_agg(...))` digests per tenant).

---

## 4. Functional Test Cases

These verify contract behavior, independent of scale. Run them against the 1M tier for speed; they are not latency tests.

**FC-01 — Health probe (EP-01, FR unmapped/ops).** `GET /api/v1/health` returns 200 with component states when all stores are up; returns 503 when the summary DB is unreachable (simulate by pausing the pg-summary container). Expected: no auth required, `cdc_worker_lag_ms` present.

**FC-02 — Counts & totals contract (EP-03 / FR-006 / PRD FR-1).** Valid request returns 200 with `data.order_count`, `gross_sales`, `net_sales`, `currency`, and a `freshness` block. Verify `exclude_cancelled=true` changes net vs gross semantics correctly.

**FC-03 — Trends contract (EP-04 / FR-007 / PRD FR-2).** `granularity=day` and `granularity=hour` both return ordered series; pagination (`page`, `page_size`) returns correct `total_buckets`; zero-count gaps are filled contiguously as the design claims.

**FC-04 — Top-N contract (EP-05 / FR-008 / PRD FR-3).** `limit` respected (default 10, bounds 1–100); `sort_by` enum (`net_sales`/`gross_sales`/`qty_sold`) changes ordering; deterministic tie-break `revenue DESC, product_id ASC`; `rank` field is 1-based and contiguous.

**FC-05 — By-status contract (EP-06 / FR-010 / PRD FR-4).** Returns one row per status present; `percentage_orders` sums to ~100 within rounding.

**FC-06 — Composite dashboard (EP-02).** Single call returns summary + status_breakdown + top_products + trends + freshness consistently (all sub-sections reflect the *same* `last_applied_lsn`).

**FC-07 — Input validation (FR-012, NFR-010, API §4).** `tenant_id < 1` → 400; `from > to` → 400; `limit > 100` → 400; span > 366 days → 400; window > 8,760 hourly buckets → 400; missing `from`/`to` → 400. Malformed ISO-8601 → 400 with structured error envelope (`code`, `message`, `request_id`, `timestamp`).

**FC-08 — Error envelope shape.** Every 4xx/5xx returns the standard envelope. Assert JSON schema, not just status code.

**FC-09 — Freshness endpoint (EP-07 / FR-017 / B-1).** Returns `as_of`, `last_applied_lsn`, `lag_ms`, `human_readable`, `status`; `status` flips to `STALE_WARNING` exactly at `lag_ms >= 30000`.

**FC-10 — Admin verification (EP-08 / B-2,B-5).** Requires admin role; non-admin → 403. Returns latency percentiles, CDC pipeline block, digest verification block.

**FC-11 — Admin rebuild (EP-09 / B-2).** `TARGETED_DIGEST_REPAIR` with `{tenant_id, day_bucket}` and `FULL_SNAPSHOT_REBUILD` both return 202 with a `job_id`; non-admin → 403; malformed mode → 400.

**FC-12 — Dev-mode auth toggle.** With `AUTH_DEV_MODE=true`, `X-Dev-Tenant-ID` matching the path succeeds while still running isolation middleware; with it false, a missing token → 401.

---

## 5. Aggregation Correctness Tests

This is the highest-priority suite. The governing rule from the design and research report: **always compare at the same LSN/snapshot**, or in-flight writes produce false mismatches (RSK-003). Use DuckDB attached to a primary snapshot as the oracle, or freeze writes and read the API's reported `last_applied_lsn`, then bound the direct query to that LSN/timestamp.

The oracle queries mirror the PRD reference SQL exactly (gross vs net semantics resolved per AMB-002).

**AC-01 — Counts & totals vs direct SQL.** For 100 random (tenant, range) pairs, assert `api.order_count == oracle.count` and `api.gross_cents == oracle.sum` and `api.net_cents == oracle.net`. Include aligned and non-aligned ranges.

```python
# tests/ac_counts.py (core assertion)
async def assert_counts(api, oracle_conn, tenant, frm, to, lsn):
    a = await api.summary(tenant, frm, to)                      # API, REPEATABLE READ at its LSN
    o = oracle_conn.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(total_amount_cents),0),
               COALESCE(SUM(CASE WHEN status<>'cancelled' THEN total_amount_cents ELSE 0 END),0)
        FROM orders
        WHERE tenant_id = ? AND created_at >= ? AND created_at < ?
    """, [tenant, frm, to]).fetchone()                          # oracle snapshot bounded to same LSN
    assert a["order_count"] == o[0], (a, o)
    assert round(a["gross_sales"]*100) == o[1]
    assert round(a["net_sales"]*100)   == o[2]
```

**AC-02 — Non-aligned boundary decomposition (the critical case, G2).** Deliberately pick ranges like `10:17:23 → 14:42:09` that cross hour and day boundaries. This exercises the hybrid decomposer (interior days + boundary hours + sub-hour edge scans). Any off-by-one in edge handling surfaces here. Run ≥50 random non-aligned ranges per tenant class.

**AC-03 — Time-series aggregation.** For both hour and day granularity, assert the API series equals the oracle `GROUP BY bucket`, including empty buckets (the API fills zeros; the oracle must be left-joined against a bucket calendar to match).

**AC-04 — Top-N correctness.** Assert the API's ordered product list (ids, qty, revenue, order_count) equals `oracle ORDER BY net_revenue DESC, product_id ASC LIMIT N`. Test each `sort_by`.

**AC-05 — Top-N under cancellation (FR-009, Yi et al. eviction case).** Cancel enough orders of the current #1 product that it should drop below #10; assert the item previously at rank 11 correctly promotes into the Top-10. This is the specific failure mode a materialized top-k would get wrong and the per-product-sum design should get right.

**AC-06 — Category breakdown.** API by-status == oracle `GROUP BY status`.

**AC-07 — Insert reflection.** Insert a known order; after lag, assert counts/sums/trend bucket increase by exactly the inserted values.

**AC-08 — Update/retraction (`placed→cancelled`, PRD FR-6, the headline case).** Flip a known order; assert `placed` bucket decrements by 1 and its amount, `cancelled` increments by 1, net_sales drops by the amount, gross unchanged, and the corresponding `agg_products_daily` net revenue retracts. Verify both hourly and daily buckets. Also test amount-change updates and `created_at`-change (bucket-move) updates.

**AC-09 — Duplicate CDC events (idempotency).** Replay the same WAL range twice (restart the worker from a stale checkpoint, or inject duplicate LSNs). Assert aggregates are unchanged after the second apply — the `last_applied_lsn FOR UPDATE` guard must discard `event_lsn <= last_applied_lsn`.

**AC-10 — Out-of-order / late-arriving events.** Apply a mutation whose `created_at` is 14 days in the past; assert the historical bucket and its digest update immediately, not after a batch refresh. Within a single tenant, LSN order is preserved by partitioning, so "out of order" here means late `created_at`, not reordered LSNs — test accordingly.

**AC-11 — Recovery & replay.** Kill the worker mid-batch; restart; assert final aggregates match a from-scratch oracle recomputation (zero double-counting, zero loss).

**AC-12 — Digest reconciliation.** Independently recompute each `(tenant, day)` digest — `sum of xxh64("{order_id}:{status}:{amount_cents}")` — on the oracle and assert it equals `bucket_digests.hash_sum_mod64`, `row_count`, and `sum_amount_cents`. This is the whole-system correctness invariant and should be run after every other AC test as a tripwire.

---

## 6. Multi-Tenant Isolation Tests

Isolation is mandatory and binary — any single leak is a hard fail (PRD FR-5, NFR-009). Tests probe application-layer predicate scoping, database RLS, and side channels.

**ISO-01 — Cross-tenant query attempt.** Token for tenant 15 requests `/tenants/42/...`; assert 403 with `TENANT_ACCESS_DENIED` *before* a DB connection is acquired. Confirm no tenant-42 bytes appear in the response or error message.

**ISO-02 — Unauthorized / missing token.** No token (with `AUTH_DEV_MODE=false`) → 401; expired/invalid JWT → 401.

**ISO-03 — Tenant ID manipulation.** Mismatched `X-Dev-Tenant-ID` vs path → rejected; negative, non-numeric, zero, and oversized `tenant_id` → 400.

**ISO-04 — RLS defense-in-depth.** Directly against pg-summary using the API's read-only role, attempt a query *without* `SET LOCAL app.current_tenant_id` and with a deliberately buggy `WHERE` (omitting tenant filter); assert RLS returns zero foreign rows. This proves the second barrier works even if the application layer has a bug.

**ISO-05 — Concurrent multi-tenant correctness.** Drive heavy concurrent traffic for tenants A and B simultaneously while mutating B's data; assert A's responses never contain B's values and remain correct. Repeat with cache enabled to catch cache-key namespace bugs (`t:{tenant_id}:...`).

**ISO-06 — Cache poisoning.** Verify Redis keys are tenant-prefixed and LSN-keyed; attempt to force a collision across tenants and assert impossibility.

**ISO-07 — Timing side channel (best-effort).** Compare response-time distributions for "tenant with data" vs "tenant with no data for range"; large systematic differences that could reveal another tenant's data volume are flagged. This is a soft check — report, don't necessarily fail.

**ISO-08 — Leakage sweep.** Automated fuzz: for many random (caller_tenant, path_tenant) pairs where they differ, assert 0 bytes of the path tenant's data ever returned. Count must be exactly zero across the entire sweep.

---

## 7. Performance Benchmarking

All performance tests run at the 10M tier unless stated, on the pinned hardware, with containers at their capped resources, CDC active, and a warm cache phase preceding measurement (see §11). Report per tenant class (whale/median/small), never a single blended number.

**Metrics and how each is measured:**

API latency (p50/p95/p99) per endpoint — measured client-side with a load tool issuing requests through the real HTTP path, so it includes auth, range decomposition, SQL, and serialization. Use a tool that reports full percentiles (`k6`, `wrk2` with corrected coordinated-omission, or `locust`). Avoid tools that only report mean.

Aggregation processing throughput — events/sec the ingest path sustains end-to-end. Measured by replaying a bounded mutation stream of known size and dividing by wall-clock from first-apply to checkpoint reaching the final LSN.

CDC processing lag — difference between a primary commit's timestamp and the moment `cdc_checkpoint.as_of_timestamp` advances past it. Measured by inserting marker rows with known commit times and polling the checkpoint.

Data freshness — commit-to-API-visibility (distinct from CDC lag, because it also includes query-side read consistency). Measured by the poll loop in §5 AC-07/AC-08.

Primary DB CPU/memory — sampled from `docker stats` and `pg_stat_*` on pg-primary during the run.

Primary query load — `pg_stat_statements` deltas on the primary: how many queries the aggregation layer actually issues against the primary (should be near-zero except sub-hour edge scans and throttled verification reads).

Primary database overhead — see §9 (its own dedicated controlled experiment).

Concurrent request performance — latency percentiles as concurrency climbs (1, 8, 20, 50, 100 clients); detect the knee where the asyncpg pool (size 20) and per-tenant semaphore (4) saturate.

Example latency harness invocation:

```bash
# k6 run, 20 virtual users, 60s, randomized tenant + range per iteration
k6 run --vus 20 --duration 60s tests/perf_summary.js \
  --summary-trend-stats="p(50),p(95),p(99)"
# perf_summary.js picks tenant from a weighted list (whale/median/small)
# and emits a Trend metric per endpoint so percentiles are reported separately.
```

A key validation inside this suite: assert that the API's self-reported `lag_ms` tracks the externally measured freshness within a tolerance. A system can lie about its own freshness; the external measurement is ground truth.

---

## 8. Scalability Tests

Run the identical latency suite at 1M, 5M, and 10M, holding all else constant, and compute ratios.

**SC-01 — Growth ratio.** For each endpoint and each tenant class, record p95 at 1M (T₁) and 10M (T₁₀). Compute T₁₀/T₁. Pass (Good) if <5×; stretch (Great) if <2×. The 5M midpoint detects non-linear inflection (e.g., when the summary working set stops fitting in the 2 GB pg-summary cache).

**SC-02 — Bucket-bound invariant.** Confirm the core design claim empirically: a whale tenant's query latency over a fixed 30-day window should be within noise of a small tenant's, because both scan the same ~120 daily summary rows. If the whale is materially slower, the bucket-bound property is violated and must be investigated.

**SC-03 — Summary table cardinality tracking.** Record actual row counts of `agg_orders_hourly`, `agg_orders_daily`, `agg_products_daily`, `bucket_digests` at each tier and compare against the design's stated bounds (daily ≤146k, hourly ≤3.5M typically <500k active, digests ≤36.5k). Divergence signals a bucketing bug.

Report as a ratio table (scenario → T₁ → T₅ → T₁₀ → ratio → verdict).

---

## 9. Primary Database Impact Tests

This directly tests the PRD's "safe for the database" goal (NFR-005: <25% Good, <10% Great) and is the experiment most prone to methodological error, so it gets a strict controlled design.

**Control (baseline, solution disabled).** Run a fixed, repeatable OLTP workload against pg-primary for a fixed duration with the CDC worker stopped, the replication slot inactive, and no dashboard traffic. Record OLTP p50/p95/p99 and primary CPU/mem. Call this L_base.

```bash
# baseline: no CDC, no dashboard
docker compose stop cdc-worker agg-api
pgbench -c 16 -j 4 -T 180 -f tests/oltp_mix.sql -P 10 "$PRIMARY_DSN" > results/L_base.txt
```

**Treatment (solution enabled).** Identical OLTP workload, but now with CDC streaming actively, 20 concurrent dashboard clients hitting the API, and the background verifier running at its throttled budget. Record OLTP p95 as L_load.

```bash
# treatment: CDC + dashboard + verifier all live
docker compose up -d cdc-worker agg-api
k6 run --vus 20 --duration 180s tests/dashboard_mix.js &   # dashboard pressure
pgbench -c 16 -j 4 -T 180 -f tests/oltp_mix.sql -P 10 "$PRIMARY_DSN" > results/L_load.txt
```

**Overhead computation.** `(L_load − L_base) / L_base` on p95. Pass <0.25, stretch <0.10.

Methodological guards that must be enforced or the number is meaningless: identical pgbench seed/script/duration/concurrency across control and treatment; verify container CPU caps are actually applied (otherwise the summary store steals the primary's cores and the result is accidentally favorable *or* unfavorable); run the pair back-to-back on a thermally stable machine; repeat the pair ≥3 times and report the distribution, not one number; and separately report how many queries the layer issued *against the primary* (`pg_stat_statements`) — the design claims near-zero except sub-hour edge scans, and that claim is independently checkable here.

Also test **PDB-02 — verifier burst impact**: run a full `FULL_SNAPSHOT_REBUILD` and a `TARGETED_DIGEST_REPAIR` during active OLTP and confirm the throttling keeps overhead within threshold even during repair — repair is the scenario most likely to breach the budget.

---

## 10. Failure and Recovery Tests

Each test defines a precise fault injection and a precise correctness/availability assertion after recovery. The universal post-condition is the §5 AC-12 digest reconciliation: after any recovery, aggregates must equal a from-scratch oracle.

**FR-01 — CDC consumer crash.** `docker kill cdc-worker` mid-stream during active writes; Compose restarts it; assert it resumes from `cdc_checkpoint.last_applied_lsn`, catches up, and final state reconciles with zero loss/duplication. Record recovery time (design claims ~1–2 s stream resume).

**FR-02 — Duplicate delivery.** Force redelivery (restart before slot ack); assert exactly-once effect (same as AC-09).

**FR-03 — Primary database restart.** Restart pg-primary; assert the API keeps serving from the summary store (degraded, possibly `STALE_WARNING`), the worker reconnects with backoff, and no WAL is lost (slot retained within `max_slot_wal_keep_size`).

**FR-04 — Summary-store failure.** Stop pg-summary; assert API returns 503 on data endpoints (and health reflects `summary_db: DOWN`); on restart, worker resumes and state is consistent.

**FR-05 — Network interruption.** Introduce latency/partition between worker and primary (e.g., `tc netem` or pausing the container); assert graceful backoff and lag reporting, then clean catch-up.

**FR-06 — Checkpoint recovery determinism.** From a known checkpoint, replay a known WAL range three times from clean restarts; assert identical final aggregates each time (determinism).

**FR-07 — WAL retention safety (RSK-001).** Keep the worker down under sustained write load; assert `max_slot_wal_keep_size=2048MB` bounds WAL growth and the slot is invalidated rather than filling the disk; then assert the DBLog catch-up/rebuild path recovers correctly.

**FR-08 — Targeted bucket repair (B-2).** Corrupt a random `(tenant, day)` summary bucket directly in pg-summary; run `/admin/verification` then `POST /admin/rebuild` TARGETED_DIGEST_REPAIR; assert only that bucket is read/repaired (measure rows read < ~1% of base, per FR-023), `is_corrupted` clears, and the bucket reconciles. Verify live CDC continued during repair (log wins on race).

**FR-09 — Full rebuild (B-2).** Truncate all summary tables; run FULL_SNAPSHOT_REBUILD; assert 100% correctness restoration against oracle and record wall-clock.

---

## 11. Benchmark Methodology

Consistency across runs is what makes results trustworthy; the following are mandatory for every performance/scalability/DB-impact test.

**Warm-up.** Discard the first 30–60 s of each measurement window so JIT, connection pools (asyncpg size 20), OS page cache, and PostgreSQL shared buffers are warm. Separately run and report **cold-cache** numbers for the first-query experience, labeled distinctly — cold and warm must never be averaged together.

**Repetitions.** Minimum 3 independent repetitions per scenario (5 for the DB-impact pair). Report median and full spread (min/max or IQR), never a single value. A result reported from one run is marked "indicative only."

**Test duration.** Latency suites: ≥500 requests per endpoint per tenant class (the PRD acceptance protocol specifies 500 randomized requests), or ≥60 s steady-state, whichever is larger. DB-impact: fixed 180 s per side. Freshness: ≥100 injected events to build a lag distribution.

**Concurrency levels.** Sweep 1, 8, 20, 50, 100 for concurrent-performance tests; use 20 as the canonical dashboard concurrency for the DB-impact pairing (matching the design's stated 20-client safety test).

**Measurement tools.** `k6`/`wrk2`/`locust` for HTTP percentiles (coordinated-omission-aware); `pgbench` for the OLTP baseline; `docker stats` + `pg_stat_statements` + `pg_stat_activity` for resource and query-load sampling; the custom poll-loop harness for freshness/CDC lag; DuckDB as correctness oracle.

**Statistical reporting.** Percentiles p50/p95/p99 for latency (means are supplementary only). For ratios (scalability, DB overhead), report the point estimate plus the range across repetitions. Flag any run where repetitions disagree by more than ~20% as unstable and re-run — laptop thermal throttling is the usual culprit and must be noted.

**Manifest.** Every run writes a manifest: hardware profile, image digests, package lockfile hash, dataset seed/rows/tenants, git SHA of system and harness, timestamp, and whether cache was warm. No result is valid without its manifest.

---

## 12. Acceptance Criteria

Exact PRD thresholds. "Good" qualifies; "Great" distinguishes. These are the binding bar; the design's internal self-claims are not acceptance criteria.

| Metric | Measurement | Good (qualify) | Great (stretch) |
|---|---|---|---|
| Query speed | Per-endpoint latency at 10M | < 1 s | < 200 ms (p95) |
| Freshness | Add/update → visible in API | < 5 min | < 30 s |
| DB safety | OLTP latency with vs without load | < 25% slower | < 10% slower |
| Correctness | API vs direct query at same LSN | Matches most | Matches all (100%) |
| Growth | Latency 1M vs 10M | < 5× slower | < 2× slower |
| Tenant isolation | Cross-tenant leakage | 100% isolated (mandatory) | 100% isolated (mandatory) |

Isolation is pass/fail only — there is no "Good" relaxation; any leak fails the whole submission.

---

## 13. Results Reporting

Results are recorded in this table. **All "Actual" and "Pass/Fail" cells remain empty until a real, manifested run produces them — nothing is pre-filled, and no numbers are invented.** Evidence links point to raw tool output, logs, or screenshots committed alongside the manifest.

| Test ID | Scenario | Expected (PRD/contract) | Actual | Pass/Fail | Evidence |
|---|---|---|---|---|---|
| AC-01 | Counts vs direct SQL, aligned | 100% exact | _TBD_ | _TBD_ | `results/ac01/*` |
| AC-02 | Counts, non-aligned boundaries | 100% exact | _TBD_ | _TBD_ | `results/ac02/*` |
| AC-05 | Top-N after #1 cancelled | #11 promotes correctly | _TBD_ | _TBD_ | `results/ac05/*` |
| AC-08 | placed→cancelled retraction | placed−1, cancelled+1, net−amt | _TBD_ | _TBD_ | `results/ac08/*` |
| AC-09 | Duplicate CDC replay | No double count | _TBD_ | _TBD_ | `results/ac09/*` |
| AC-12 | Digest reconciliation | All (tenant,day) match | _TBD_ | _TBD_ | `results/ac12/*` |
| ISO-01 | Cross-tenant 403 | 403 before DB conn, 0 leak | _TBD_ | _TBD_ | `results/iso01/*` |
| ISO-08 | Leakage fuzz sweep | 0 bytes leaked | _TBD_ | _TBD_ | `results/iso08/*` |
| PERF-03 | EP-03 p95 @10M (whale) | <1 s / <200 ms | _TBD_ | _TBD_ | `results/perf/ep03_whale` |
| PERF-05 | EP-05 p95 @10M (median) | <1 s / <200 ms | _TBD_ | _TBD_ | `results/perf/ep05_med` |
| SC-01 | 1M→10M p95 ratio | <5× / <2× | _TBD_ | _TBD_ | `results/scale/*` |
| FRSH-01 | Insert→visible lag | <5 min / <30 s | _TBD_ | _TBD_ | `results/fresh/insert` |
| FRSH-02 | Cancel→visible lag | <5 min / <30 s | _TBD_ | _TBD_ | `results/fresh/cancel` |
| PDB-01 | OLTP overhead @20 clients | <25% / <10% | _TBD_ | _TBD_ | `results/dbimpact/*` |
| FR-01 | CDC crash recovery | Resume, 0 loss/dup | _TBD_ | _TBD_ | `results/recover/cdc` |
| FR-08 | Targeted bucket repair | <1% rows read, reconciles | _TBD_ | _TBD_ | `results/repair/targeted` |

Separation of planned vs executed: this document's tables are the *planned* suite. An executed run produces a dated copy (`results/RUN_<date>/scorecard.md`) with the manifest attached; only that copy may contain real numbers.

---

## 14. Demo Validation

A single reproducible `run_demo.sh` that a judge executes in one command and that proves the core value proposition end to end. The demo is scripted so it is identical on every machine.

Sequence: (1) `docker compose up` and seed the 10M/100-tenant dataset, printing base-table row counts and distinct tenant count as proof of scale. (2) Fire the four must-have endpoints for a whale tenant and show p95 latency printed live (speed). (3) Insert a new order and a `placed→cancelled` update via script, then poll the API and print the measured commit-to-visibility lag alongside the API's self-reported `lag_ms` (freshness + honesty of the watermark). (4) Run the correctness comparator for 100 random non-aligned ranges against the DuckDB oracle and print "100% exact" or the first mismatch (correctness). (5) Attempt a cross-tenant request and show the 403 (isolation). (6) Corrupt one `(tenant, day)` bucket, run `/admin/verification` to detect it and `/admin/rebuild` to repair it, printing rows-read vs a full rebuild (self-healing, B-2). (7) Run the OLTP baseline-vs-loaded pair and print the overhead percentage (DB safety). Each step prints a PASS/FAIL against the PRD column it targets, producing a live scorecard.

The demo must fail loudly if any invariant breaks — a demo that silently continues past a mismatch is worse than no demo.

---

## 15. Risks and Testing Limitations

The most consequential risk is measuring correctness against a moving target: comparing the API to a direct query while writes flow produces false mismatches (RSK-003). Mitigation is strict LSN/snapshot alignment in every AC test; if alignment can't be guaranteed, that test is marked inconclusive rather than pass.

Laptop variance is a first-order confound. Thermal throttling, background processes, and power state can swing latency by tens of percent, which is larger than the gap between "Good" and "Great." Mitigation: AC power, stable thermals, multiple repetitions, report spread, and treat any >20% inter-repetition variance as a re-run trigger.

The container CPU caps are load-bearing for the DB-safety claim but are easy to misconfigure or silently ignore on some Docker backends (notably Docker Desktop on macOS/Windows uses a VM whose cgroup enforcement differs from native Linux). If caps aren't truly enforced, the overhead number is invalid. Mitigation: verify enforcement with a CPU-burn test before trusting PDB results, and note the Docker backend in the manifest.

Dependency and ambiguity risks: the organizer's generator schema (shared-table vs per-tenant, presence of `order_items`, gross-vs-net sales semantics — AMB-001/002/004, DEP-001) can change the ground-truth queries. Until the official generator is in hand, all AC results from the synthetic generator are provisional and must be re-validated against the official dataset.

Coverage limitations to state plainly: this plan does not benchmark the MongoDB fallback path (PostgreSQL is the primary target); it does not prove behavior beyond 10M rows (the PRD ceiling), so the "scales 10×" claim is validated only at 1M→10M, not toward the aspirational cloud numbers in the design; the timing side-channel test (ISO-07) is best-effort and cannot rule out all micro-architectural channels; and the "max events per batch" bound referenced conceptually in the design is not specified numerically in the source material, so throughput tests measure the realized coalescer behavior rather than verifying a documented cap. Finally, single-active-CDC-consumer means HA rests on restart recovery, not active-active — the recovery-time measurement (FR-01) is therefore a load-bearing result, not a nice-to-have.

A closing methodological commitment consistent with the instructions: no number in this plan is a result. Every quantitative cell is a placeholder to be filled only by a manifested, repeatable run, and any figure reported later must cite its raw evidence and manifest or be labeled indicative.