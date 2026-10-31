# Xponent — Synthetic Data Strategy

**Status:** Strategy proposal. No organizer-provided dataset exists; the team generates its own representative data for development, testing, and the demo. No application code written yet; no source documents modified; architecture unchanged.
**Date:** 2026-10-31
**Companion:** `IMPLEMENTATION_PLAN.md` (tasks T2 seeder + scripts), `OPEN_DECISIONS.md` (D-series / O-series), `TECHNICAL_GAP_ANALYSIS.md`, `PROGRESS.md`.

> **Key consequence of owning the data:** our own base tables, queried by **direct SQL at a quiesced snapshot, are the authoritative correctness oracle** — by construction. We do not need an external dataset to prove exactness; we need a generator that is *deterministic* (so mismatches are reproducible) and *representative* (so the rollups are actually exercised). This also means the organizer-dependent questions O1–O5 become decisions we make ourselves (see `OPEN_DECISIONS.md`).

Legend used below: **[FACT]** = taken directly from the five source documents; **[ASSUMPTION]** = our working assumption, changeable; **[RECOMMENDATION]** = our proposal pending team ratification.

---

## 1. Synthetic data model

### 1.1 Entities (mirror the approved schema — do not invent new tables)
**[FACT]** The DB/API Spec (tables 1–5) already defines the base schema: `tenants`, `products`, `customers`, `orders`, `order_items`. We generate data **into that exact schema** — no schema changes.

| Entity | Fields that matter for the rollups | Why it is necessary |
|---|---|---|
| `tenants` | `tenant_id`, `plan_tier` | Tenant isolation (FR-5); `plan_tier` feeds the per-tenant rate limiter (B-3). |
| `products` | `product_id`, `tenant_id`, `product_name`, `unit_price_cents` | Top-N products (FR-3); per-tenant catalog cardinality drives query-time ranking cost. |
| `customers` | `customer_id`, `tenant_id` | FK realism for orders. Not aggregated in MVP (distinct-customer is a P2 holistic metric). Keep minimal. |
| `orders` | `order_id`, `tenant_id`, `customer_id`, `status`, `total_amount_cents`, `created_at`, `updated_at` | The core fact. `created_at` → time bucket (FR-1/2); `status` → category breakdown (FR-4) + retraction (FR-6); `total_amount_cents` → SUM; `updated_at` → freshness. |
| `order_items` | `order_item_id`, `order_id`, `tenant_id`, `product_id`, `quantity`, `line_amount_cents`, `order_created_at` | Drives per-product rollups (FR-3). `tenant_id` + `order_created_at` are **denormalized** onto items **[FACT: DB spec table 5]** so the CDC worker can bucket product deltas without joining `orders`. |

### 1.2 Relationships necessary to test the rollups
- **[FACT]** `tenant → {products, customers, orders}` (1:N); `order → order_items` (1:N); each item references one `product`.
- **[RECOMMENDATION]** Enforce the invariant **`orders.total_amount_cents == SUM(order_items.line_amount_cents)` per order** at generation time. This gives the correctness harness a cheap internal consistency check and makes the gross/net SUM rollups verifiable against both tables.
- **[RECOMMENDATION]** Denormalize `tenant_id` and `order_created_at` onto `order_items` (already in the spec) so product rollup bucketing never needs a join.

### 1.3 Distributional properties that the generator MUST produce (so tests aren't vacuous)
- **[FACT]** Pareto/power-law tenant skew — a few "whale" tenants (1M–3M orders) vs median (~20k–50k) vs small (~10k). Needed for NFR-003 skew resilience and the bucket-bound invariant (SC-02).
- **[FACT]** Timestamps spread across a multi-month UTC window. Needed for trends (FR-2) and range decomposition.
- **[RECOMMENDATION — important]** **Sufficient sub-hour / sub-minute density**: enough orders inside individual hours and minutes that a non-aligned range (e.g. `10:17:23 → 14:42:09`) actually contains boundary rows. Without this, AC-02 (the critical non-aligned test) passes vacuously and proves nothing. Add a `--sub-hour-density` knob that guarantees edge buckets are non-empty for the demo tenant.
- **[RECOMMENDATION]** Load-time status is mostly `placed`/`shipped`; **cancellations are applied via the mutation stream**, not baked in at load, so `placed→cancelled` retraction (FR-6, AC-08) is tested as a live transition.
- **[ASSUMPTION]** Modest per-tenant product catalog (hundreds–low thousands of SKUs) so query-time Top-N ranking stays cheap and AC-05 (#11 promotes when #1 is cancelled) is testable. (We control this; set via `--products-per-tenant`.)
- **[RECOMMENDATION]** A small fraction of orders carry multiple line items (mix of 1–N items) so product rollups and order totals diverge meaningfully.

---

## 2. Deterministic generator

**[FACT]** The Test Plan already sketches `tools/gen_dataset.py` (Pareto, bulk `COPY`) and `tools/gen_mutations.py` (replayable update/cancel stream with recorded commit timestamps). **[RECOMMENDATION]** Reuse that design as the planned `scripts/seed_dataset.py`, `scripts/gen_mutations.py`, `scripts/apply_mutations.py` (tasks T2 + scripts in `IMPLEMENTATION_PLAN.md`) — do not design a new approach.

### 2.1 Two artifacts
1. **Base loader (`seed_dataset.py`)** — deterministic bulk `COPY` of tenants/products/customers/orders/order_items. Produces a static dataset for bootstrap.
2. **Mutation stream (`gen_mutations.py` → file → `apply_mutations.py`)** — a replayable sequence of INSERTs, status UPDATEs (incl. `placed→cancelled`), and amount changes, each recording its commit timestamp at apply time. This is the ground truth for freshness, retraction, and replay tests.

### 2.2 Determinism rules
- **[RECOMMENDATION]** All randomness flows from a single seeded RNG (`--seed`). Same `(seed, rows, tenants, span, products, cancel-frac, skew-alpha, sub-hour-density, generator git SHA)` → byte-identical base tables, verified by a per-tenant digest (`SELECT md5(string_agg(...))` or `xxhash`). Log the full parameter set + git SHA into a run manifest; no run is valid without its manifest.

### 2.3 Configurable parameters
| Flag | Purpose | Example values |
|---|---|---|
| `--rows` | total order count | 50_000 / 1_000_000 / 5_000_000 / 10_000_000 |
| `--tenants` | tenant count | 5 / 10 / 100 |
| `--seed` | reproducibility | 42 |
| `--span-days` | UTC time window | 120 |
| `--products-per-tenant` | catalog size | 1_000 |
| `--cancel-frac` | share eventually cancelled (via mutation stream) | 0.06 |
| `--skew-alpha` | Pareto skew strength | 1.16 |
| `--sub-hour-density` | guarantee non-empty edge buckets for the demo tenant | on/off or orders-per-hour floor |
| `--items-per-order` | distribution of line items per order | 1–5 |
| `--dsn` | target primary DB | — |

### 2.4 Performance note on generation (not a benchmark result)
**[ASSUMPTION]** At 10M orders + items with `REPLICA IDENTITY FULL`, bulk `COPY` plus WAL will take on the order of minutes to tens of minutes on a laptop — this is a *generation-time* estimate, **not** a measured system performance result. Mitigation: generate the 10M volume once into a reusable Docker volume (pre-seeded) and keep the live demo on the 1M tier.

---

## 3. Testing without an organizer dataset

**The oracle:** because we generate the base tables, a **direct SQL aggregation over `orders`/`order_items` at a quiesced snapshot is ground truth by definition.** Every correctness test compares the API against that direct query at the same point in history (pause the mutation stream, let CDC drain to caught-up, then compare — **[FACT]** DuckDB cannot read "as of LSN", so we quiesce rather than time-travel).

| What we test | How (using only our own data) | Maps to |
|---|---|---|
| **Inserts** | Append a known order via the mutation stream; poll API; assert count/sum/trend bucket increase by exactly the inserted values. | AC-07, FR-6 |
| **Updates / cancellations** | Mutation stream flips a known order `placed→cancelled` (and amount/`created_at` changes); assert `placed−1`, `cancelled+1`, net drops by amount, gross unchanged, product net retracts per chosen semantics; check hourly **and** daily buckets. | AC-08, FR-6 |
| **Replay / duplicate CDC** | Restart the worker from a stale checkpoint or inject duplicate LSNs; assert aggregates unchanged (per-event LSN dedupe, fix H1). | AC-09/AC-11 |
| **Tenant isolation** | Multi-tenant data lets us drive cross-tenant probes: token/path mismatch → 403 before DB conn; RLS blocks a deliberately unscoped query (ISO-04); fuzz many `(caller, path)` pairs → 0 bytes leaked. | ISO-01..08, FR-5 |
| **Aligned ranges** | Harness picks whole-day/whole-hour boundaries; API == direct SQL. | AC-01 |
| **Non-aligned ranges** | Harness deliberately picks sub-hour boundaries (e.g. `10:17:23→14:42:09`); `--sub-hour-density` guarantees non-empty edges; API == direct SQL. **This is the binding correctness bar — aligned-only does not satisfy it.** | AC-02 |
| **Digest reconciliation** | Independently recompute per-`(tenant, day)` digest on base tables; assert equals `bucket_digests`. Run as an after-every-test tripwire. | AC-12 |

**[RECOMMENDATION]** The generator's mutation stream carries the *expected* delta for each operation, so freshness tests measure commit→visible lag against a known commit timestamp, and retraction tests assert exact expected bucket values — no external reference needed.

---

## 4. Recommended data volumes (volumes, NOT performance results)

> **These are data sizes we will generate. They are not benchmark results.** No latency, freshness, or DB-overhead number may be stated until a manifested, repeatable run produces it (Test Plan discipline). The table below says nothing about how fast the system is.

| Tier | Orders | Tenants | Purpose | **[source]** |
|---|---|---|---|---|
| Dev inner-loop | 50k–100k | 5 | Fast local iteration (seconds to seed) | **[RECOMMENDATION]** |
| Functional / correctness | 1M | 10 | Run the full AC + ISO + FC suites repeatedly | **[FACT: PRD small run]** |
| Scaling midpoint | 5M | 100 | Detect non-linear inflection before 10M (SC-01) | **[FACT: Test Plan]** |
| Demo default | 1M | 10 | Fast, reproducible bring-up for judges | **[RECOMMENDATION — D5]** |
| Headline benchmark | 10M | 100 | PRD main run, from a pre-seeded volume | **[FACT: PRD main run]** |

**[FACT]** The PRD requires the system to be demonstrated at 10M/100 tenants (with a 1M/10 small run). The dev and midpoint tiers are our additions for velocity and inflection detection.

---

## 5. How this resolves the organizer-dependent questions

Because we own the dataset and the reference queries, O1–O5 are no longer external unknowns; we decide them and our oracle enforces them. See `OPEN_DECISIONS.md` for the authoritative status. Summary:

- **O1 gross vs net:** we define it. Schema already carries both `gross_*` and `net_*`; our oracle tests both; demo default = gross. → N/A (no organizer); ratify default.
- **O2 tenant topology:** we use shared tables with a `tenant_id` column. → N/A; decided.
- **O3 order_items present:** yes, we generate them (needed for FR-3). → N/A; decided.
- **O4 timestamp alignment:** our harness deliberately tests both aligned and non-aligned. → N/A; requirement retained and strengthened.
- **O5 generator DB engine:** PostgreSQL (confirmed). → N/A; decided.

**Single contingency:** if the organizers later publish a reference query or dataset/requirements, O1 (semantics) and O3 (schema shape) must be re-aligned to match theirs before final submission. Tracked as a note, not an open blocker.

---

## 6. Assumptions that still need team approval

- **[ASSUMPTION]** Modest per-tenant catalog (hundreds–low thousands of SKUs).
- **[RECOMMENDATION]** Add the `--sub-hour-density` and `--items-per-order` knobs so non-aligned and multi-item cases are non-vacuous.
- **[RECOMMENDATION]** Enforce the `order total == Σ line items` invariant at generation time.
- **[RECOMMENDATION]** Demo default = gross sales semantics (net carried alongside). *(Ratify with D2.)*
- Data volume tiers in §4. *(Ratify with D5.)*

None of these change the architecture or the five source documents.
