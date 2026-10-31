# Xponent — Technical Gap Analysis

**Status:** Independent audit of the five approved specs. Findings to burn down before/early in implementation. No source documents modified; architecture unchanged.
**Date:** 2026-10-31
**Companion:** `IMPLEMENTATION_PLAN.md` (roadmap), `OPEN_DECISIONS.md` (decisions), `PROGRESS.md` (status).

> These are defects and omissions in the **reference code and cross-document consistency** — not in the architecture, which is sound. Several would fail the spec's own acceptance tests if shipped as-is. Each finding names the document location, the problem, the impact, and the fix.

---

## 1. Critical findings

### C1 — Sub-hour edge scan specified but not executed → non-aligned ranges undercount
- **Where:** DB/API Spec §7.2 (`fetch_tenant_summary` / `decompose_range`); contradicts Architecture §5.3 and Test Plan AC-02.
- **Problem:** `decompose_range` computes `left_edge` / `right_edge`, but `fetch_tenant_summary` only queries interior days + boundary full hours — it never executes the residual sub-hour edge query. A request `from=10:17` floors to the 11:00 hour bucket and silently drops 10:17–11:00.
- **Impact:** Fails AC-02, the single most heavily-weighted correctness test. Any non-hour-aligned timestamp returns wrong counts/sums.
- **Fix:** Implement the residual sub-hour edge query (LSN-bounded index-only scan on primary `idx_orders_tenant_created`, `asyncio.gather`'d with the interior query), **or** add a minute-grain summary bucket so edges never touch the primary. See OPEN_DECISIONS D1 (recommend minute-bucket). Route all four endpoints through one shared decomposer.

### C2 — Product rollups never maintained by live CDC
- **Where:** DB/API Spec §8 (`MicroBatchCoalescer`); contradicts Architecture §4.5.
- **Problem:** The worker handles only order hourly/daily rollups. There is no `order_items` handler, so `agg_products_daily` is populated only at bootstrap and never updates from live inserts/cancellations.
- **Impact:** Fails AC-05 (Top-N after #1 cancelled) and FR-3 under FR-6 (live Top-N).
- **Fix:** Add an `order_items` CDC path emitting qty/revenue deltas into `agg_products_daily`.

### C3 — Product-revenue retraction on cancellation has no data path
- **Where:** DB/API Spec §6 / `shadow_orders`; Architecture G1/§4.6; Test Plan AC-05.
- **Problem:** When an order flips `placed→cancelled`, the WAL event is on `orders` and carries no line items. Retracting `net_revenue_cents` in `agg_products_daily` needs that order's products, but `shadow_orders` stores no line items and the worker does no lookup.
- **Impact:** "Net sales excludes cancelled" for products is unimplemented; AC-05 net-revenue path fails.
- **Fix:** For MVP, define Top-N and total sales on **gross** (cancellation does not retract product revenue) and align the oracle; carry `net_*` columns so semantics can switch once the organizer's reference query is known. Item-aware shadow state (store line items, join status) is P2 only. See OPEN_DECISIONS D2.

### C4 — `SET LOCAL app.current_tenant_id` outside a transaction is a no-op → RLS returns zero rows
- **Where:** DB/API Spec §7.3 (trends/top/by-status endpoints); Architecture §8.2.
- **Problem:** Those endpoints run `conn.execute("SET LOCAL ...")` with no surrounding transaction. `SET LOCAL` is scoped to the current transaction (which auto-commits immediately), so it has no effect; the RLS policy `current_setting('app.current_tenant_id', true)::INTEGER` then evaluates NULL and filters **all** rows. Separately, the API connects as `agg_user` (table owner), and RLS does not apply to the owner without `FORCE ROW LEVEL SECURITY`.
- **Impact:** Endpoints return empty; or (as owner) RLS is silently bypassed. Either way isolation + correctness are broken.
- **Fix:** Wrap every tenant read in an explicit transaction that first runs `SET LOCAL app.current_tenant_id`; connect the API as a non-owner `agg_reader` role; `ALTER TABLE ... FORCE ROW LEVEL SECURITY`. Add ISO-04 (prove RLS blocks a deliberately unscoped query).

---

## 2. High-severity findings

### H1 — Exactly-once guard filters per-batch, not per-event
- **Where:** DB/API Spec §8 `flush_batch_to_summary`; Architecture §4.7.
- **Problem:** Discards a batch only when `batch.max_lsn <= last_applied_lsn`. Replication does not guarantee identical batch boundaries across restarts, so a replayed batch with a new `max_lsn` that also contains already-applied events double-counts the old ones.
- **Impact:** Fails AC-09 (duplicate replay) in the boundary-crossing case.
- **Fix:** Dedupe per event by LSN (drop `event.lsn <= last_applied_lsn` individually) before coalescing; keep the atomic checkpoint advance.

### H2 — Bootstrap snapshot ↔ stream handoff not coordinated
- **Where:** Architecture §4.1 vs DB/API Spec bootstrap SQL (§9.3).
- **Problem:** Architecture prescribes `CREATE_REPLICATION_SLOT ... EXPORT_SNAPSHOT` → seed at L0 → stream from L0. The reference bootstrap is a plain `GROUP BY` with no slot/snapshot coordination; writes between seed and stream start cause gaps or double counts.
- **Impact:** Bootstrap correctness not guaranteed if writes are live during seeding.
- **Fix:** Create the slot first, seed from its exported snapshot (or seed a quiesced static dataset, then start the worker), record L0 in `cdc_checkpoint`. Seed-quiesced-then-stream is accepted for the demo (OPEN_DECISIONS D4).

### H3 — `bucket_digests` never populated or maintained
- **Where:** DB/API Spec bootstrap + worker; Architecture §9.2 / ADR-005.
- **Problem:** Neither the bootstrap SQL nor the worker writes `bucket_digests`. The verification/repair differentiator (B-2 — our flagship) has nothing to verify.
- **Impact:** B-2 cannot function; AC-12 digest reconciliation has no data.
- **Fix:** Populate digests at bootstrap; maintain `hash_sum_mod64` incrementally (`+hash(new) − hash(old)` mod 2^64) in the same atomic batch as the rollups.

### H4 — Trends & by-status do no range decomposition and truncate to whole days
- **Where:** DB/API Spec §7.3; contradicts PRD FR-1/2/4 exactness.
- **Problem:** `granularity=day` casts `from`/`to` to `.date()`, so partial first/last days are wrong for non-aligned ranges, and these endpoints never read hourly/edge data.
- **Impact:** AC-02/03/06 fail for non-aligned ranges.
- **Fix:** Route trends and by-status through the same shared decomposer as C1 (interior days + boundary hours + edges). One decomposition module for all four endpoints.

### H5 — Idle freshness watermark inflates `lag_ms` with no writes
- **Where:** DB/API Spec §7.2 freshness block; Architecture §6.1.
- **Problem:** `lag_ms = now − as_of_timestamp`, and `as_of` advances only on applied commits. With no new writes the system looks increasingly stale although it is caught up — bad in a judge demo (insert one order, wait, watch "lag" climb).
- **Impact:** Demo-quality / honesty of the watermark; may trip the 30 s `STALE_WARNING` boundary spuriously.
- **Fix:** Worker heartbeat updates `as_of_timestamp` to the primary's current commit time (or server clock) when the slot reports caught-up / zero pending WAL.

### H6 — Scope exceeds the PRD (auth + several differentiators)
- **Where:** DB/API Spec §4 (JWT) vs PRD OOS-002; SRS FR-019/FR-020; Architecture DRR/config/Mongo/DuckDB.
- **Problem:** JWT Bearer auth is mandated though the PRD excludes login (OOS-002); DRR fairness, config metrics, Mongo fallback, and the DuckDB oracle are all more than a 48h team can build at quality.
- **Impact:** Build-time + debugging risk for zero scoring benefit; over-scope endangers the core.
- **Fix:** Tenant identity via path + dev header (+ optional static `X-Tenant-Key`); keep isolation middleware + RLS; drop JWT. Defer DRR/config/Mongo/DuckDB to P2. (Confirmed scope decision.)

---

## 3. Medium findings (fix if cheap / be aware)

- **M1 — created_at-change ("bucket move") updates** use a single `created_at` for both retraction and insertion; the retraction needs `old_row.created_at`. Rare; AC-08 tests it. Fix in the worker update path.
- **M2 — DuckDB cannot read "as of LSN."** Correctness comparison must run on a quiesced DB (pause mutations, let CDC drain, compare) or via a PG repeatable-read snapshot — not DuckDB time-travel. Bake into the demo/runbook.
- **M3 — Edge scans hit the primary**, slightly undercutting "never touch primary." The minute-grain bucket (D1) removes this. Note the trade-off in the demo.
- **M4 — 10M seeding time** (COPY of 10M orders + items + `REPLICA IDENTITY FULL` WAL) can be tens of minutes on a laptop — a judge-reproducibility risk. Provide a 1M default demo and a pre-seeded volume / background seed for 10M.
- **M5 — `synchronous_commit=off`** on the summary store makes the "atomic exactly-once" really at-least-once + idempotent replay (which is correct). Describe accurately; do not over-claim durability.
- **M6 — Stale references** to `Report_for_Multitenant.md`; the actual file is `Reserarch.md` (note the misspelling). Cosmetic.

---

## 4. Cross-document consistency matrix

| Gap ID | Where | Existing decision | Problem | Severity | Resolution | Blocks impl? |
|---|---|---|---|---|---|---|
| G-01 | DB §7.2 vs Arch §5.3, Test AC-02 | Hybrid edge decomposition | Edge query computed, never executed (C1) | **Critical** | Implement edges or minute-bucket | Yes |
| G-02 | DB §8 worker vs Arch §4.5 | Product rollups on CDC | `order_items`/product path missing (C2) | **Critical** | Add order_items handler | Yes (FR-3 live) |
| G-03 | DB shadow_orders vs Arch G1 | Retraction before-image | No line items → product retraction impossible (C3) | **Critical** | Define Top-N gross, or store items | Yes (AC-05) |
| G-04 | DB §7.3 vs Arch §8.2 | RLS tenant isolation | `SET LOCAL` no-op; owner bypasses RLS (C4) | **Critical** | Txn-wrap + agg_reader + FORCE RLS | Yes (returns empty) |
| G-05 | DB §8 worker vs Arch §4.7 | Exactly-once | Per-batch LSN filter double-counts (H1) | High | Per-event LSN dedupe | Yes (AC-09) |
| G-06 | Arch §4.1 vs DB bootstrap SQL | Snapshot↔stream handoff | No slot/snapshot coordination (H2) | High | Slot-first + L0 checkpoint | Yes |
| G-07 | DB tables vs Arch §9.2/ADR-005 | Digest verification | Digests never populated/maintained (H3) | High | Populate + maintain digests | Yes (differentiator) |
| G-08 | DB §7.3 vs PRD FR-1/2/4 | Non-aligned correctness | trends/by-status truncate to days (H4) | High | Shared decomposer | Yes (AC) |
| G-09 | API §4 vs PRD OOS-002 | Auth | JWT mandated; PRD excludes login (H6) | High | Path + dev header, drop JWT | No (scope) |
| G-10 | Arch §6 vs DB freshness code | Idle watermark | lag inflates with no writes (H5) | High | Heartbeat `as_of` | No (demo quality) |
| G-11 | SRS FR-019/020; Arch DRR/config | DRR, config metrics | Unrealistic in 48h at quality | Med | Defer to P2 | No |
| G-12 | Test §5 vs DuckDB capability | LSN-aligned oracle | DuckDB can't read as-of-LSN (M2) | Med | Quiesce-then-compare | No |
| G-13 | Arch/CON-004 vs confirmed scope | Mongo fallback | Dual CDC doubles effort | Med | PG-only; Mongo only if generator is Mongo | No |

**Categories:** Actual contradictions = G-04, G-09, G-13. Missing details / unimplemented = G-01, G-02, G-03, G-05, G-06, G-07, G-08. Engineering recommendations = G-10, G-12, M3, M4. Optional improvements = G-11.

---

## 5. Fix ordering (burn-down)

1. Before the vertical slice: C4 (RLS correctness), H2 (bootstrap handoff), H1 (per-event idempotency) — these are foundational and cheap to get right up front.
2. During breadth (CP2): C1 + H4 (shared decomposer + non-aligned exactness), C2 (product rollups), C3 decision (gross semantics).
3. During differentiator (CP3): H3 (digests) + verify/repair.
4. Demo quality: H5 (heartbeat), M2/M3 (oracle + edge trade-off notes).
5. Awareness only: M1, M4, M5, M6.
