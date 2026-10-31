# Xponent — Open Decisions

**Status:** Decision log. `CONFIRMED` items are settled; `RECOMMENDED — AWAITING APPROVAL` need a team sign-off before the affected work starts; `OPEN` items depend on the organizer and must stay open until kickoff.
**Date:** 2026-10-31 (updated for team-owned synthetic dataset)
**Companion:** `IMPLEMENTATION_PLAN.md`, `TECHNICAL_GAP_ANALYSIS.md`, `PROGRESS.md`, `SYNTHETIC_DATA_PLAN.md`.

> No decision here changes the approved architecture. Each records: the decision, the recommended option, alternatives, impact, and status.
>
> **Dataset update:** there is **no organizer-provided dataset**. The team generates its own deterministic synthetic data (`SYNTHETIC_DATA_PLAN.md`), so our own base tables + direct SQL are the authoritative correctness oracle. This converts the former organizer-dependent questions (O1–O5) into decisions we make ourselves, and lets several D-series items be settled with our own oracle rather than waiting on an external party.

---

## A. Decisions recommended, awaiting team approval

### D1 — Edge handling for non-aligned time ranges
- **Decision:** How to answer `from`/`to` timestamps that don't align to hour/day bucket edges, exactly.
- **Recommended:** Add a **minute-grain summary bucket** (`agg_orders_minutely`) so every endpoint decomposes to day + hour + minute entirely inside the summary store; the primary is never touched for edges.
- **Alternatives:** (a) LSN-bounded index-only **edge scan on the primary** per request (Architecture's original design) — correct, but touches the primary and slightly weakens the "DB-safe" story; (b) restrict the API to aligned ranges — rejected, fails AC-02.
- **Impact:** Adds one summary table + its maintenance in the worker. Removes primary edge reads (helps NFR-005). Slightly more storage. Resolves C1/G-01 and M3.
- **Status:** **APPROVED** (team, 2026-10-31). Build the minute-grain bucket; do not query the primary for edges.

### D2 — Top-N / total-sales semantics under cancellation (AMB-002)
- **Decision:** Do Top-N product sales and total sales count cancelled orders (gross) or exclude them (net)?
- **Recommended:** Use **gross for MVP** (cancellation does not retract product revenue); carry `net_*` columns in every summary table so both are available.
- **Alternatives:** Net-only (requires item-aware shadow state + product retraction on cancel — C3 option a, P2 effort); support both via a query flag (already partly in the schema; costs test surface).
- **Impact:** Keeps C3 out of the critical path.
- **Now decidable with our own oracle:** No organizer dataset — **we define the semantics and our direct-SQL oracle is authoritative for whichever we pick.** No longer contingent on O1.
- **Status:** **APPROVED** (team, 2026-10-31). Default = gross; carry net columns. Resolves C3/G-03.

### D3 — Authentication
- **Decision:** What auth the API enforces.
- **Recommended:** **Drop JWT.** Tenant via URL path + `X-Dev-Tenant-ID` (dev mode) + optional static `X-Tenant-Key`; still run the isolation middleware + RLS.
- **Alternatives:** HMAC-JWT Bearer (as written in the DB/API spec) — build + debug cost for no scoring benefit, and contradicts PRD OOS-002.
- **Impact:** Less code, lower risk; isolation unaffected (middleware + RLS remain). Resolves H6/G-09.
- **Status:** **APPROVED** (team, 2026-10-31). Drop JWT; path + dev header + optional static key; keep isolation middleware + RLS.

### D4 — Bootstrap vs live-stream handoff
- **Decision:** How to coordinate the initial snapshot seed with the start of CDC streaming.
- **Recommended:** **Seed on a quiesced primary, create the slot, record L0, then start the worker** (static-first). Live mutations only after the worker is streaming.
- **Alternatives:** Full `EXPORT_SNAPSHOT` handoff with concurrent writes (Architecture §4.1) — correct but more moving parts; only needed if the dataset must be written while seeding.
- **Impact:** Removes the handoff race for the demo. Resolves H2/G-06.
- **Now decidable with our own generator:** we control the generator, so we can guarantee a quiesced seed before streaming — this approach is fully in our hands.
- **Status:** **APPROVED** (team, 2026-10-31). Seed-quiesced-then-stream.

### D5 — Demo scale tiers
- **Decision:** What scale the live demo runs at vs what the headline benchmark reports.
- **Recommended:** **Default demo at 1M/10 tenants** (fast, reproducible for judges); **10M/100 tenants** as the headline benchmark from a pre-seeded volume.
- **Alternatives:** Demo live at 10M — risks a multi-minute seed/bring-up in front of judges (M4).
- **Impact:** Protects judge reproducibility; 10M numbers still reported with a manifest.
- **Now decidable with our own generator:** we generate all tiers ourselves (`SYNTHETIC_DATA_PLAN.md §4`); sizes are entirely our choice. These are data **volumes**, not performance results.
- **Status:** **APPROVED** (team, 2026-10-31). Demo 1M/10; headline benchmark 10M/100; dev 50k–100k/5; 5M/100 midpoint.

---

## B. Confirmed decisions

### C-D6 — Scope ceiling
- **Decision:** Rock-solid core (P0) + **one** differentiator = digest verification + targeted repair (B-2). DRR, config metrics, Mongo, DuckDB oracle are P2.
- **Status:** CONFIRMED (this session).

### C-D7 — Primary database
- **Decision:** **PostgreSQL only** (logical decoding via `pgoutput` + `REPLICA IDENTITY FULL`). The Mongo change-streams adapter is a kickoff contingency, built only if the organizer generator targets Mongo — not a parallel build.
- **Status:** CONFIRMED (this session). Revisit only if O1/O-gen shows Mongo.

### C-D8 — Implementation stack
- **Decision:** **Python end-to-end** — FastAPI + asyncpg API, psycopg3 logical-replication worker. Matches the reference code.
- **Status:** CONFIRMED (this session).

### D6 — CDC logical-decoding plugin (implementation, made during T4)
- **Decision:** Which logical-decoding output plugin the worker consumes.
- **Chosen:** **wal2json** — structured JSON changes with full before-images under `REPLICA IDENTITY FULL`, reliably parsed in Python (via psycopg2 logical replication). Requires a Debian-based primary image with `postgresql-16-wal2json` (`db/primary/Dockerfile`); the bootstrap creates the slot with the `wal2json` plugin.
- **Alternatives:** `pgoutput` (built-in, no custom image, but binary protocol — error-prone to hand-decode and untestable without a live DB); `test_decoding` (text, built-in, but not designed for consumption). The transport is isolated in `worker/cdc_processor.py`, so swapping back to `pgoutput` later touches only that file.
- **Impact:** pg-primary now builds a custom image; the `agg_layer_pub` publication (pgoutput-specific) is currently unused but left in place harmlessly.
- **Status:** IMPLEMENTED (team may override; cheap to swap). Needs live-DB validation.

---

## C. Former organizer-dependent questions — now resolved by us (no organizer dataset)

> There is **no organizer-provided dataset**. We generate our own data and define our own reference queries, so these are no longer external unknowns — we decide them and our direct-SQL oracle enforces them. Each is marked **N/A — no organizer dataset**, with the decision we have taken.
>
> **Single contingency:** if the organizers later publish a reference query or dataset/requirements, O1 (semantics) and O3 (schema shape) must be re-aligned to theirs before final submission. Tracked as a note, not an open blocker.

### O1 — Gross vs net sales semantics (AMB-002)
- **Now:** We define it. Schema carries both `gross_*` and `net_*`; oracle tests both; demo default = gross (see D2).
- **Status:** N/A — no organizer dataset. Decision folded into D2. *(Contingency if organizers later publish a reference query.)*

### O2 — Physical tenant topology (AMB-001) — REOPENED then RESOLVED (reviewer, 2026-10-31)
- **Reviewer clarification:** each tenant may have its **own source database**, separate per tenant; Xponent is the central platform ingesting from all of them.
- **Now:** Supported via a **tenant-source registry** (`common/registry.py`, env `TENANT_SOURCES`). Xponent runs one CDC stream + one slot per tenant database, all writing into the single central, `tenant_id`-keyed summary. The summary/API/RLS/decomposer/Receipts are unchanged (topology-agnostic — they only read the central summary). Verify/repair/edge reads resolve each tenant to its source DB via the registry. **Default (unset) = single shared source**, preserving the original prototype.
- **MVP representation:** per-tenant DBs are separate databases in the primary cluster (`tenant_1`, `tenant_2`), provisioned by `scripts/provision_tenant_dbs.py`, seeded with `seed_dataset.py --assign-tenant`. Production note: separate instances/hosts.
- **Status:** RESOLVED — per-tenant-DB topology implemented and opt-in; shared-source default retained.

### O3 — Order-to-product relationship (AMB-004)
- **Now:** We **include `order_items`** (child table), required for FR-3 Top-N; in the CDC publication; drives `agg_products_daily`.
- **Status:** N/A — no organizer dataset. Decided. *(Contingency if organizers later publish a different schema.)*

### O4 — Timestamp alignment & timezone (AMB-003)
- **Now:** Our correctness harness **deliberately tests both aligned and non-aligned** UTC ranges; `--sub-hour-density` guarantees non-empty edges so AC-02 is non-vacuous. Buckets are UTC.
- **Status:** N/A — no organizer dataset. Requirement retained and strengthened.

### O5 — Database engine
- **Now:** **PostgreSQL** (confirmed, C-D7). Generator targets Postgres.
- **Status:** N/A — no organizer dataset. Decided.

---

## D. Summary

| ID | Decision | Recommended | Status |
|----|----------|-------------|--------|
| D1 | Non-aligned edge handling | Minute-grain summary bucket | **APPROVED** (2026-10-31) |
| D2 | Top-N / sales semantics | Gross for MVP, net columns carried | **APPROVED** (2026-10-31) |
| D3 | Auth | Drop JWT; path + dev header | **APPROVED** (2026-10-31) |
| D4 | Bootstrap handoff | Seed-quiesced-then-stream | **APPROVED** (2026-10-31) |
| D5 | Demo/benchmark scale | 1M demo / 10M benchmark (volumes, not results) | **APPROVED** (2026-10-31) |
| C-D6 | Scope ceiling | Core + B-2 differentiator | CONFIRMED |
| C-D7 | Primary DB | PostgreSQL only | CONFIRMED |
| C-D8 | Stack | Python end-to-end | CONFIRMED |
| O1 | Gross vs net | We define; default gross (see D2) | N/A — no organizer dataset |
| O2 | Tenant topology | Shared table w/ tenant_id | N/A — no organizer dataset (decided) |
| O3 | order_items present? | Yes, we generate them | N/A — no organizer dataset (decided) |
| O4 | Timestamp alignment | Test both; aligned + non-aligned | N/A — no organizer dataset (retained) |
| O5 | Generator DB engine | PostgreSQL | N/A — no organizer dataset (decided) |

**All D-series decisions (D1–D5) APPROVED by the team on 2026-10-31.** No decisions remain open. O1–O5 are N/A (no organizer dataset) with the single re-alignment contingency noted above. Implementation is unblocked; see `PROGRESS.md` for next actions. (Tasks remain `not started` until the team gives the explicit go-ahead to begin coding.)
