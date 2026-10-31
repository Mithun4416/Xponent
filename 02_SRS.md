This Software Requirements Specification (SRS) defines the functional, non-functional, and architectural requirements for the **Multi-Tenant Live Aggregation Layer**, engineered for the 48-hour cybersecurity and systems hackathon.

## 1. Introduction

### 1.1 Purpose

The purpose of this Software Requirements Specification (SRS) is to provide a complete, unambiguous, and testable engineering specification for building a high-performance, multi-tenant data aggregation layer. It serves as the primary implementation reference for developers, system architects, and QA/benchmarking engineers during the 48-hour hackathon build cycle.

### 1.2 Scope

The system under specification is an intermediate **Multi-Tenant Aggregation Layer** positioned between a primary transactional database (OLTP) and tenant-facing analytical dashboards/APIs. In the context of an e-commerce store platform (analogous to Shopify), where individual store owners act as tenants with orders, products, and customers, the software ingests live data changes, maintains pre-aggregated rollups, and serves sub-second analytical queries via a REST API without degrading primary database read/write performance.

### 1.3 Intended Audience

* **Software Engineers & Architects:** To design the Change Data Capture (CDC) ingestion pipeline, summary storage schema, retraction logic, and query routing API.


* **QA & Benchmarking Engineers:** To execute the verification harness covering the 1M and 10M record datasets, tenant isolation checks, and OLTP degradation tests.


* **Hackathon Judges & Evaluators:** To verify compliance with mandatory ("Must-Have") and optional ("Bonus") requirements and evaluate performance against "Good" (Qualifying) and "Great" (Stretch) targets.



### 1.4 Definitions and Terminology

* **Requirement Keywords (RFC 2119):**
* **MUST:** Indicates an absolute mandatory requirement derived directly from the organizer's PRD.


* **SHOULD:** Indicates a strongly recommended architectural capability or high-value hackathon bonus feature that differentiates the solution.


* **MAY:** Indicates an optional enhancement or discretionary implementation detail.




* **Tenant:** An individual customer organization or store owner operating on the platform whose data must remain logically isolated from other tenants.


* **Incremental View Maintenance (IVM):** The technique of updating derived materialized aggregates by computing and applying only the delta changes ($\Delta$) rather than re-scanning base tables.


* **Z-Set / Retraction:** A data model (formalized in DBSP) representing database modifications as weighted records: insertions carry weight $+1$, deletions carry weight $-1$, and updates are decomposed into a retraction of the prior state (old row, $-1$) plus an insertion of the new state (new row, $+1$).


* **Before-Image (Pre-Image):** The state of a database record immediately prior to an `UPDATE` or `DELETE` operation, required to subtract prior values from aggregate buckets.


* **Freshness Watermark (`as_of`):** A monotonic timestamp or log sequence number (LSN) indicating the exact point in transactional history up to which the aggregation layer reflects all committed primary database writes.


* **DBLog Watermark Protocol:** A lock-free chunked snapshot and repair technique that interleaves bounded table reads with live change-log events by bracketing chunks with low and high watermarks, applying the rule that the live log wins on race conditions.



### 1.5 References

1. **Authoritative Requirement Baseline:** *PRD: Multi-Tenant Aggregation Layer — Problem, Goals and Requirements*, Aroop Ghosh (28 October 2026).


2. **Technical Research Reference:** *Multi-Tenant Live Rollups at Scale: Research Report for the Mitigata Aggregation-Layer PRD* (`Report_for_Multitenant.md`).


3. **Standard Reference:** IEEE Std 830 / ISO/IEC/IEEE 29148 Systems and Software Engineering — Life Cycle Processes — Requirements Engineering.

---

## 2. Problem Understanding

### 2.1 Existing Problem

Multi-tenant SaaS platforms (such as multi-store e-commerce engines) must provide store owners with live operational dashboards showing order counts, revenue totals, temporal sales trends, top-selling products, and order status breakdowns. While direct `SELECT ... GROUP BY` queries against the primary transactional database execute instantaneously over a few thousand rows, they fail when data scales to millions of records across hundreds of unevenly distributed tenants.

### 2.2 Current System Limitations

* **Severe Query Latency:** Executing ad-hoc analytical queries over millions of rows requires full-index or full-table scans, causing dashboard load times to degrade from milliseconds to seconds or minutes.


* **Cross-Tenant "Noisy Neighbor" Degradation:** Heavy analytical queries executed by a single large tenant consume shared database CPU, memory buffer pools, and disk I/O, slowing down normal transactional reads and writes (checkout, order placement) for all tenants on the platform.


* **False Dichotomy of Naive Shortcuts:** Relying on nightly batch jobs keeps the database safe during the day but serves stale (yesterday's) numbers; querying live transactional tables provides fresh data but incurs prohibitive database load.



### 2.3 Root Causes

1. **Row-Count Dependency:** Direct aggregation complexity scales linearly ($O(N)$) with the number of historical orders per tenant rather than being bounded by time buckets.


2. **Resource Contention:** Sharing the same storage and execution engine for latency-sensitive OLTP point-writes and throughput-heavy OLAP scans causes buffer cache eviction and lock contention.


3. **Tenant Data Skew:** In real-world multi-tenant distributions, a small fraction of "whale" tenants generate the vast majority of records, creating severe hotspots during both querying and maintenance.



### 2.4 Business Impact

Slow dashboards degrade merchant trust and decision-making, while primary database slowdowns directly impact checkout conversion rates, increase transaction timeouts, and breach platform Service Level Agreements (SLAs) for all hosted stores.

### 2.5 Why Conventional Database Aggregation is Insufficient

As documented in the technical research report (`Report_for_Multitenant.md`), standard off-the-shelf database mechanisms fail to meet all PRD goals simultaneously:

* **Native Materialized Views (`REFRESH MATERIALIZED VIEW`):** Replaces the entire view via a full recompute scan, creating massive periodic load spikes on the primary database and bounding data freshness by the total duration of the refresh.


* **Synchronous Trigger-Based IVM (e.g., `pg_ivm`):** Maintains views immediately inside the write transaction via `AFTER` triggers, which adds synchronous latency to every `INSERT`/`UPDATE` (e.g., degrading write times from 9 ms to 15 ms in documented benchmarks) and acquires an `ExclusiveLock` on grouped views, serializing concurrent tenant order writes and violating the "Safe for the database" goal.


* **Append-Only Analytical Views (e.g., ClickHouse Incremental MVs):** Fire only on block inserts and cannot automatically derive retractions when an existing order's status changes (e.g., `placed` $\rightarrow$ `cancelled`) without custom upstream CDC state management.


* **Time-Bucketed Continuous Aggregates (e.g., TimescaleDB):** Lag on historical row modifications (such as status updates to older orders) until background refresh policies execute, and consume primary instance CPU unless replicated externally.



---

## 3. Proposed Solution Overview

### 3.1 High-Level Solution Description

(Note: The PRD permits any technical approach and stack. The architecture below represents our proposed technical design informed by `Report_for_Multitenant.md`.)

We propose an asynchronous, **CDC-Driven Incremental Rollup Engine** decoupled from the primary OLTP database. The architecture consists of four logical tiers:

1. **Asynchronous CDC Ingestion & Retraction Engine:** Reads the primary database's write-ahead log (PostgreSQL Logical Decoding via `pgoutput`/`wal2json` or MongoDB Change Streams) asynchronously off the transactional write path. It maintains a lightweight shadow state (or consumes database pre-images) to convert row modifications into Z-set weighted deltas: $+1$ for inserts, $-1$ for deletes, and $(-1 \text{ old row}, +1 \text{ new row})$ for updates.


2. **Pre-Aggregated Summary Store:** A dedicated, resource-isolated datastore holding fine-grained summary tables keyed by `(tenant_id, time_bucket, dimension)` alongside transactional CDC offset checkpoints (LSN/resume token) committed atomically to guarantee idempotency.


3. **Hybrid Range-Decomposing Query API:** A lightweight HTTP/REST service that enforces strict tenant scoping, decomposes arbitrary `from`/`to` time windows into pre-aggregated interior buckets plus exact boundary edges, and attaches freshness watermarks (`as_of`, `lag_ms`) to every response.


4. **Anti-Entropy Verification & Fair Maintenance Scheduler (Proposed Differentiators):** A background verifier that compares lightweight per-`(tenant, day)` cryptographic/hash digests against throttled source snapshots and repairs corrupted buckets using DBLog-style watermarked chunking, paired with Deficit Round Robin (DRR) per-tenant queues to prevent large-tenant backfills from starving small tenants.



```
+-------------------------------------------------------------------------+
|                        PRIMARY OLTP DATABASE                            |
|       (PostgreSQL Logical Decoding / MongoDB Change Streams)            |
+------------------------------------+------------------------------------+
                                     |
        1. Asynchronous CDC Stream   |   4. Throttled Edge / Digest Reads
           (Zero Sync Write Lock)    |      (Replica or Snapshot-Bracketed)
                                     v
+-------------------------------------------------------------------------+
|                   AGGREGATION & INGESTION ENGINE                        |
|  +---------------------------+   +-----------------------------------+  |
|  | Tenant-Fair DRR Scheduler |-->| Z-Set Delta & Retraction Processor|  |
|  | (Per-Tenant Ingest Queues)|   | (Shadow State / Before-Image)     |  |
|  +---------------------------+   +-----------------+-----------------+  |
+----------------------------------------------------|--------------------+
                                                     |
                       2. Atomic Transaction         |
                          (Deltas + LSN Offset +     |
                           Bucket Hash Digests)      v
+-------------------------------------------------------------------------+
|                     ISOLATED SUMMARY DATASTORE                          |
|  - agg_orders_hourly (tenant_id, hour_bucket, status, count, sum_sales) |
|  - agg_products_daily (tenant_id, day_bucket, product_id, qty, revenue) |
|  - shadow_orders (order_id -> tenant_id, created_at, status, amount)    |
|  - bucket_digests (tenant_id, day_bucket, count, sum, hash_sum, lsn)    |
+------------------------------------+------------------------------------+
                                     ^
                       3. Bounded    |   5. DBLog-Style Targeted
                          Bucket     |      Bucket Repair
                          Reads      |
+------------------------------------+------------------------------------+
|                REST API & VERIFICATION SERVICE LAYER                    |
|  - Tenant Isolation Filter & Per-Tenant Token Bucket Rate Limiter       |
|  - Hybrid Range Decomposer (Full Interior Buckets + Exact Edge Slices)  |
|  - Freshness Watermark Decorator (as_of, lag_ms) & /monitor UI          |
+-------------------------------------------------------------------------+

```

### 3.2 Core Capabilities

* **Exact Distributive & Algebraic Rollups:** Pre-computes `COUNT` and `SUM` across tenant, time (hour/day), status, and product dimensions so query cost scales with the number of time buckets rather than raw row count.


* **Retraction-Correct Updates:** Accurately decrements prior status/amount buckets and increments new buckets when orders transition states (e.g., `placed` $\rightarrow$ `cancelled`).


* **Arbitrary Time-Range Precision:** Splits non-aligned `from`/`to` timestamps into pre-computed middle buckets plus exact edge evaluations so answers match direct database queries 100% of the time.


* **Zero-Lock Primary Database Safety:** Keeps all aggregation compute off the synchronous OLTP write path, restricting primary DB overhead to well under the 10% "Great" threshold.



### 3.3 Expected Outcomes

* Sub-200 ms dashboard query latencies at 10 million records across 100 skewed tenants.


* Sub-30 second end-to-end data visibility lag from order creation/update to API reflection.


* 100% mathematical equivalence with direct SQL/NoSQL ground-truth queries at matching snapshot LSNs.



---

## 4. Functional Requirements

(Note: Requirements derived from the PRD "Must Have" table are designated **MUST**. PRD "Bonus" features are designated **SHOULD**. Architectural mechanisms recommended by the research report to satisfy PRD targets are labeled **[Proposed Architectural Requirement]**.)

### 4.1 Multi-Tenant Data Ingestion

* **FR-001 (CDC Stream Ingestion) [Proposed Architectural Requirement]:** The system MUST asynchronously ingest committed data modifications (`INSERT`, `UPDATE`, `DELETE`) from the primary database containing `orders`, `products`, and `customers` (and order line items if generated) without placing synchronous triggers or exclusive locks on primary transactional tables.


* **FR-002 (Idempotent Offset Tracking) [Proposed Architectural Requirement]:** The ingestion engine SHOULD persist the source database log position (PostgreSQL LSN or MongoDB Resume Token) atomically within the same transaction as the summary table delta updates, ensuring exactly-once aggregate effects across restarts or redeliveries.


* **FR-003 (Initial Dataset Bootstrap):** The system MUST support bootstrapping initial aggregate state from the organizer-provided dataset generator for both the 1 million record (10 tenants) and 10 million record (100 tenants) configurations.



### 4.2 Live Aggregation and Rollups

* **FR-004 (Time-Bucketed Pre-Aggregation) [Proposed Architectural Requirement]:** The system MUST maintain pre-aggregated rollup structures partitioned by `tenant_id` and temporal granularity (at minimum hourly and daily buckets) to serve dashboard queries without scanning base transactional tables.


* **FR-005 (Arbitrary Time Range Decomposition) [Proposed Architectural Requirement]:** When a query specifies `from` and `to` timestamps that do not align with pre-aggregated bucket boundaries (e.g., `10:17` to `14:42`), the system MUST decompose the query into full interior buckets plus exact boundary edge evaluations so the returned aggregate matches a direct base-table query.



### 4.3 Counts, Totals, Trends, Top-N, and Category Breakdowns

* **FR-006 (Counts and Totals for Time Range — PRD FR-1):** The system MUST provide an API capability that returns the total number of orders (`COUNT`) and total sales revenue (`SUM`) for a specified tenant over any requested `[from, to]` time range (e.g., number of orders and total sales in the last 7 days).


* **FR-007 (Trend Over Time — PRD FR-2):** The system MUST provide an API capability that returns a time-series breakdown of metrics (e.g., orders and/or sales per hour or per day) for a specified tenant over a requested `[from, to]` window (e.g., orders per day for the last 30 days).


* **FR-008 (Top-N List — PRD FR-3):** The system MUST provide an API capability that returns the Top-$N$ entities ranked by a specified measure within a requested `[from, to]` time window for a given tenant (specifically including Top 10 products by sales this week).


* **FR-009 (Top-N Under Cancellations/Deletions) [Proposed Architectural Requirement]:** To prevent Top-$N$ inaccuracy when order cancellations or updates reduce a product's sales total, the system SHOULD maintain exact per-`(tenant_id, time_bucket, product_id)` revenue and quantity sums and compute the final `ORDER BY ... LIMIT N` ranking at query time.


* **FR-010 (Breakdown by Category — PRD FR-4):** The system MUST provide an API capability that returns order metrics grouped by a categorical dimension for a specified tenant and time range (specifically including orders broken down by status: e.g., `placed`, `shipped`, `cancelled`).



### 4.4 Tenant Filtering and Isolation

* **FR-011 (Mandatory Tenant Filter — PRD FR-5):** Every dashboard and analytical query endpoint MUST require and enforce an explicit tenant identifier (`tenant_id`), ensuring that Store A can never view, infer, or aggregate Store B's data.


* **FR-012 (Missing or Cross-Tenant Request Rejection):** The API MUST reject any request lacking a valid `tenant_id` parameter with an HTTP `400 Bad Request` or `403 Forbidden` status code, and MUST scope all underlying summary store reads strictly by the authenticated/requested `tenant_id` prefix.



### 4.5 Handling Inserts, Updates, and Incremental Aggregation

* **FR-013 (Live Reflection of New Inserts — PRD FR-6):** When a new order is inserted into the primary database, the system MUST incrementally update all relevant tenant rollup buckets so that subsequent queries reflect the increased count and sales totals within the required freshness window.


* **FR-014 (Live Reflection of Updates and State Transitions — PRD FR-6):** When an existing record is modified in the primary database (e.g., an order status changes from `placed` to `cancelled`, or an order amount is adjusted), the system MUST incrementally adjust the affected aggregates—decrementing the prior category/bucket and incrementing the new category/bucket.


* **FR-015 (Before-Image / Shadow State Resolution) [Proposed Architectural Requirement]:** To execute retractions accurately under `UPDATE` and `DELETE` events, the ingestion pipeline MUST obtain the prior state of modified rows either via database pre-image configuration (e.g., PostgreSQL `REPLICA IDENTITY FULL` or MongoDB `changeStreamPreAndPostImages`) or via a local shadow state table keyed by `order_id`.



### 4.6 API Access and Bonus Capabilities

* **FR-016 (Simple REST API & Documentation — PRD FR-7):** The system MUST expose a clean HTTP REST API (e.g., `GET /tenants/{tenant_id}/orders/count?from=...&to=...`) accompanied by a concise `README.md` documenting all endpoints, query parameters, and execution instructions.


* **FR-017 (Data Freshness Metadata — PRD Bonus B-1):** Every API response SHOULD include freshness watermark metadata indicating how current the returned numbers are (e.g., `"as_of": "2026-10-31T14:00:00Z"`, `"lag_seconds": 4`, or `"freshness": "updated 4 seconds ago"`).


* **FR-018 (Per-Tenant Query Concurrency / Rate Limiting — PRD Bonus B-3):** The system SHOULD enforce per-tenant query admission control (such as a token-bucket rate limiter or concurrent query semaphore) to limit how many heavy analytical queries a single tenant can execute simultaneously.


* **FR-019 (Tenant-Fair Background Maintenance) [Proposed Enhancement for B-3]:** The ingestion and backfill pipeline SHOULD schedule per-tenant maintenance work using fair queuing (e.g., Deficit Round Robin across `tenant_id` partitions) so a massive tenant's backfill or burst does not starve live updates for smaller tenants.


* **FR-020 (Declarative Config-Driven Metrics — PRD Bonus B-4):** The system MAY allow operators to define and register a new distributive or algebraic metric (e.g., `COUNT`, `SUM`, `AVG` decomposed into `SUM/COUNT`) by editing a declarative configuration file (YAML/JSON) rather than modifying application source code, while automatically rejecting non-incremental holistic metrics (such as exact median).


* **FR-021 (Operational Monitoring Page — PRD Bonus B-5):** The system SHOULD expose a lightweight monitoring endpoint or web page (`/monitor`) displaying real-time query execution latencies (p50/p95/p99), CDC ingestion lag (`lag_ms`), and verification status.



### 4.7 Data Verification and Recovery

* **FR-022 (State Rebuild from Scratch — PRD Bonus B-2):** The system SHOULD provide an administrative command or endpoint capable of rebuilding all pre-computed numbers from scratch (or per tenant) if summary state becomes corrupted or out of sync, without locking the primary database.


* **FR-023 (Digest Verification & Targeted Bucket Repair) [Proposed Enhancement for B-2]:** The system SHOULD maintain incremental per-`(tenant_id, day)` verification digests (tracking `count`, `sum`, and modular hash sum of order records) and support DBLog-style watermark-bracketed chunk repair to selectively rebuild only mismatched `(tenant_id, day)` buckets without performing a full 10M-row table scan.



---

## 5. Non-Functional Requirements

### 5.1 Performance

* **NFR-001 (Dashboard Query Latency):** At 10 million records across 100 tenants, every mandatory dashboard query (FR-006 through FR-010) MUST complete in **under 1 second (< 1,000 ms)** to meet the Qualifying ("Good") target, and SHOULD complete in **under 200 milliseconds (< 200 ms)** to achieve the Stretch ("Great") target.



### 5.2 Scalability

* **NFR-002 (1M to 10M Record Growth Scaling):** When scaling the dataset 10x from the small test run (1 million records, 10 tenants) to the main run (10 million records, 100 tenants), dashboard query execution time MUST degrade by **less than 5x** ("Good" target) and SHOULD degrade by **less than 2x** ("Great" target).


* **NFR-003 (Skewed Tenant Distribution Resilience):** Query latency for an individual tenant MUST be governed by the number of time buckets in the requested range rather than the tenant's raw order count, ensuring both small tenants (e.g., 10k orders) and skewed "whale" tenants (e.g., 2M+ orders) meet NFR-001 latency bounds.



### 5.3 Freshness

* **NFR-004 (End-to-End Visibility Lag):** The elapsed time between committing a new order or status update in the primary database and its reflection in API query responses MUST be **under 5 minutes (< 300 seconds)** ("Good" target) and SHOULD be **under 30 seconds (< 30 seconds)** ("Great" target).



### 5.4 Primary Database Safety & Overhead

* **NFR-005 (OLTP Workload Protection):** Under active dashboard query load and continuous background aggregation, normal application transactional query latency on the primary database MUST slow down by **less than 25%** ("Good" target) and SHOULD slow down by **less than 10%** ("Great" target) compared to baseline OLTP performance without the aggregation layer.


* **NFR-006 (Replication Log Safety) [Proposed Architectural Requirement]:** If PostgreSQL logical replication slots are used, the system MUST configure bounded WAL retention (e.g., `max_slot_wal_keep_size`) so a stalled consumer cannot exhaust primary disk space.



### 5.5 Consistency and Correctness

* **NFR-007 (Aggregate Accuracy vs. Ground Truth):** Numbers returned by the aggregation layer MUST match the results of an equivalent direct query against the primary database at the same snapshot/LSN on **most queries** ("Good" target) and SHOULD match **100% of queries** ("Great" target), including queries covering arbitrary time boundaries and updated/cancelled orders.


* **NFR-008 (No Approximate Sketch Drift) [Proposed Architectural Requirement]:** Because exact matching is scored, the system MUST use exact distributive/algebraic rollup arithmetic rather than approximate probabilistic sketches (such as HyperLogLog or Space-Saving) for FR-1 through FR-4.



### 5.6 Security and Tenant Isolation

* **NFR-009 (Strict Logical Data Isolation):** A tenant MUST have zero visibility into another tenant's orders, revenue, products, or aggregate buckets under any combination of API parameters, error messages, or timing side-channels.


* **NFR-010 (Input Validation & Injection Prevention):** All API inputs (`tenant_id`, `from`, `to`, `limit`, `granularity`) MUST be strictly validated and parameterized to prevent SQL/NoSQL injection and cross-tenant parameter tampering.



### 5.7 Reliability, Maintainability, and Resource Efficiency

* **NFR-011 (Single-Laptop Resource Envelope):** The entire stack (primary DB, CDC processor, summary store, API server) MUST execute reliably within the CPU, RAM, and disk constraints of a standard developer laptop holding 10 million records.


* **NFR-012 (Reproducible Turnkey Deployment):** Judges MUST be able to build, start, seed, and verify the complete system by following the repository `README.md` (using `docker compose up` or a single bootstrap script).



---

## 6. System Constraints

### 6.1 Hardware Constraints

* **CON-001 (Laptop Hardware Execution):** All services, databases, and benchmark scripts MUST run locally on a single standard laptop without relying on external cloud clusters or distributed server racks.


* **CON-002 (Container CPU/Memory Isolation) [Proposed Architectural Constraint]:** To guarantee NFR-005 (Primary DB Safety) on a single host machine, the summary store and CDC worker containers SHOULD be constrained via Docker CPU/memory limits (`cpus`, `mem_limit`) so background aggregation cannot starve the primary OLTP database container of host CPU cycles.



### 6.2 Software Constraints

* **CON-003 (Open Technology Stack):** The PRD permits any programming language, database, and caching technology. However, heavyweight distributed JVM analytics clusters (e.g., multi-node Apache Druid or Apache Pinot with ZooKeeper) SHOULD be avoided due to laptop memory exhaustion risks.


* **CON-004 (Database Compatibility):** The ingestion architecture MUST align with the database engine targeted by the organizer's data generation script (expected to be PostgreSQL or MongoDB).



### 6.3 Time Constraints

* **CON-005 (48-Hour Hackathon Window):** Design, implementation, data seeding (1M and 10M rows), verification harness execution, and documentation MUST be completed within the 48-hour hackathon window.



### 6.4 Dataset Requirements

* **CON-006 (Mandatory Dataset Tiers):** The system MUST be tested and demonstrated against two official organizer-generated dataset tiers:


1. **Small Test Run:** 1,000,000 records across 10 tenants.


2. **Main Run:** 10,000,000 records across 100 tenants, featuring realistic power-law/Pareto skew where a few tenants hold orders of magnitude more data than the median tenant.





### 6.5 Hackathon Demonstration Constraints

* **CON-007 (Judge Reproducibility):** The submission MUST include clear step-by-step startup instructions in `README.md` and an automated benchmark/verification script that visually proves latency, freshness, DB safety, correctness, and scaling targets to the judges.



---

## 7. Performance and Acceptance Criteria

The table below preserves the exact metrics and thresholds from Section 4 of the organizer's PRD, distinguishing Qualifying (**"Good"**) targets from Stretch (**"Great"**) targets, and pairs them with specific verification protocols.

| Metric Category | PRD Measurement Definition | Qualifying Target ("Good") | Stretch Target ("Great") | Concrete Verification & Acceptance Protocol |
| --- | --- | --- | --- | --- |
| **Scale & Tenancy** | Dataset volume loaded on a normal laptop

 | 10M records, 100 tenants (plus 1M / 10-tenant small run)

 | 10M records, 100 skewed tenants running smoothly on laptop

 | Verify row counts in base tables (`SELECT COUNT(*)`, `COUNT(DISTINCT tenant_id)`) prior to running benchmark suite.

 |
| **Query Latency** | Time for each dashboard query (FR-1 to FR-4) at 10M records

 | **< 1 second** (< 1,000 ms)

 | **< 200 ms**

 | Execute 500 randomized requests per endpoint across small, median, and largest tenants (cold & warm); assert p95 and p99 latency $< 200\text{ ms}$.

 |
| **Data Freshness** | Time from adding/updating an order to seeing it in API numbers

 | **< 5 minutes** (< 300 s)

 | **< 30 seconds**

 | Inject a stream of live `INSERT` and `UPDATE` (`placed` $\rightarrow$ `cancelled`) transactions while polling the API every 500 ms; measure delta between commit timestamp and API reflection.

 |
| **Primary DB Overhead** | Normal app OLTP query time with vs. without dashboard load

 | **< 25% slower**

 | **< 10% slower**

 | Run a baseline OLTP read/write workload (`pgbench` or generator write replay) for 60s without aggregation/dashboard load ($L_{\text{base}}$), then repeat with 20 concurrent dashboard API clients + live CDC active ($L_{\text{load}}$). Verify $\frac{L_{\text{load}} - L_{\text{base}}}{L_{\text{base}}} < 0.10$.

 |
| **Aggregation Correctness** | Compare aggregation API numbers with a direct database query

 | **Matches on most queries**

 | **Matches on all queries (100%)**

 | Freeze writes or evaluate at a consistent snapshot LSN (`as_of`). Run 100 randomized queries (aligned and non-aligned `from`/`to` timestamps, post-update states) against both the API and direct SQL/DuckDB oracle. Assert 100% exact numeric equivalence.

 |
| **Tenant Isolation** | Store A can never see Store B's numbers

 | **100% Isolated** (Mandatory FR-5)

 | **100% Isolated** (Mandatory FR-5)

 | Automated security test: mutate Tenant B's orders heavily while querying Tenant A; attempt missing/invalid `tenant_id` calls; assert 0 bytes of cross-tenant data leakage.

 |
| **Growth Scaling** | Query speed at 1M records (10 tenants) vs. 10M records (100 tenants)

 | **< 5x slower**

 | **< 2x slower**

 | Record p95 query latency across all 4 endpoints at 1M rows ($T_{1\text{M}}$) and 10M rows ($T_{10\text{M}}$). Verify $\frac{T_{10\text{M}}}{T_{1\text{M}}} < 2.0$.

 |

---

## 8. User Roles and Use Cases

### 8.1 User Roles

1. **Tenant / Store Owner:** A customer operating one or more stores on the platform who views analytical metrics (orders, revenue, trends, top products, status breakdowns) scoped strictly to their own store(s).


2. **Dashboard / API Consumer:** Front-end web applications, automated test harnesses, Postman clients, or Jupyter notebooks invoking the REST API to fetch JSON rollups and freshness watermarks.


3. **System Operator / Site Reliability Engineer (SRE):** Technical staff monitoring query latency, CDC replication lag, per-tenant rate limits, and triggering bucket verification or state rebuilds.


4. **Platform Administrator:** Privileged user capable of registering new declarative metrics via configuration files (Bonus B-4) and provisioning tenant quotas.



### 8.2 Core Use Cases

* **UC-01: Query Live Store Dashboard Metrics (Actor: Tenant / API Consumer)**
* *Preconditions:* Tenant `42` has historical and live orders in the system.


* *Trigger:* Consumer issues `GET /tenants/42/orders/summary?from=2026-10-24T00:00:00Z&to=2026-10-31T14:00:00Z` (plus parallel calls for `/trends`, `/products/top?limit=10`, and `/orders/by-status`).


* *Flow:* API validates `tenant_id=42`, checks the per-tenant token bucket, decomposes the `[from, to]` range into full daily/hourly buckets plus exact boundary slices, merges the results, and attaches the current `as_of` watermark.


* *Postconditions:* Exact counts, totals, trends, top-10 products, and status breakdowns are returned in $< 200\text{ ms}$ without querying the primary OLTP database's hot transactional path.




* **UC-02: Live Order Status Update & Retraction (Actor: Primary App / CDC Engine)**
* *Preconditions:* Order `#9001` for Tenant `42` currently has status `placed` and amount `$150.00`.


* *Trigger:* Customer cancels Order `#9001` in the primary database (`UPDATE orders SET status = 'cancelled' WHERE order_id = 9001`).


* *Flow:* CDC consumer captures the change event, retrieves the before-image (`status = 'placed'`, `$150.00`), emits a retraction $(-1, -\$150.00)$ to the `(tenant=42, bucket, status='placed')` rollup and an insertion $(+1, +\$150.00)$ to `(tenant=42, bucket, status='cancelled')`, and updates the LSN watermark atomically.


* *Postconditions:* Within $< 30\text{ seconds}$, querying `/tenants/42/orders/by-status` shows `placed` decremented by 1 and `cancelled` incremented by 1, matching a direct DB query.




* **UC-03: Corrupt Bucket Detection and Targeted Rebuild (Actor: System Operator)**
* *Preconditions:* Summary bucket for Tenant `42` on `2026-10-12` experiences simulated drift or corruption (Bonus B-2).


* *Trigger:* Operator triggers `POST /admin/verify` or `POST /admin/rebuild?tenant_id=42&day=2026-10-12`.


* *Flow:* Verifier compares the summary bucket digest against the source digest, flags `(tenant=42, 2026-10-12)`, and executes a DBLog watermark-bracketed chunked scan to recompute that specific bucket while live CDC continues streaming.


* *Postconditions:* Aggregate state returns to 100% accuracy without locking the primary database or scanning all 10 million rows.





---

## 9. Assumptions and Dependencies

*(Note: All items below are explicitly labeled to avoid conflating architectural assumptions with organizer requirements.)*

* **ASM-001 [Technical Assumption — Primary Database Engine]:** Based on the PRD's suggested tools list, we assume the organizer's data generator targets either **PostgreSQL** or **MongoDB**. Our architecture supports PostgreSQL logical decoding (`pgoutput`) as primary choice and MongoDB Change Streams (`changeStreamPreAndPostImages`) as drop-in fallback.


* **ASM-002 [Technical Assumption — Schema Layout]:** Although Section 1 of the PRD colloquially mentions *"Each tenants' orders, products and customers sit in their respective database"*, Section 3 (FR-5: *"Tenant filter on every query"*) and Section 4 (100 tenants on one laptop) strongly imply a shared database / shared schema layout with a `tenant_id` discriminator column.


* **ASM-003 [Technical Assumption — Product Catalogue Cardinality]:** We assume per-tenant product cardinality in the generated dataset is modest (hundreds to tens of thousands of SKUs per tenant rather than millions), making exact per-`(tenant, day, product)` sum maintenance and query-time Top-$N$ ranking highly efficient without requiring complex $k'$-buffer maintenance.


* **DEP-001 [External Dependency — Organizer Data Generator]:** The team depends on the organizer's data generation script to produce the official `orders`, `products`, and `customers` schemas and data distributions for the 1M and 10M runs.


* **DEP-002 [Environment Dependency — Docker Runtime]:** The evaluation laptop is assumed to have Docker and Docker Compose installed with sufficient disk space (~15–25 GB free) to store 10 million base records plus WAL/oplog and summary tables.



---

## 10. Out-of-Scope Features

In strict accordance with Section 2 ("Not required") of the PRD and the findings in `Report_for_Multitenant.md`, the following features are explicitly **OUT OF SCOPE**:

* **OOS-001 (Polished Production Frontend UI):** A complex React/Angular multi-page SaaS portal is not required; a simple HTML monitoring page, Postman collection, curl script, or Jupyter notebook is fully sufficient.


* **OOS-002 (Authentication, Billing, and IAM):** User login flows, OAuth/OIDC identity providers, RBAC user management, and payment processing are excluded.


* **OOS-003 (Cloud Infrastructure Deployment):** Kubernetes clusters, AWS/GCP/Azure managed cloud deployments, and multi-region geo-replication are excluded; everything runs locally on a laptop.


* **OOS-004 (Machine Learning & Predictive Forecasting):** Demand forecasting, anomaly detection, or ML-based recommendations are excluded.


* **OOS-005 (Approximate Query Processing / Sketches):** Probabilistic data structures (HyperLogLog, Count-Min Sketch, Space-Saving) are excluded because they fail under interleaved order cancellations and violate the 100% exact-match correctness target.



---

## 11. Risks and Open Questions

### 11.1 Technical & Operational Risks

* **RSK-001 (WAL / Oplog Disk Exhaustion on Consumer Stall):** If the CDC consumer crashes or stalls during the 10M record run, PostgreSQL logical replication slots will retain WAL files indefinitely by default (`max_slot_wal_keep_size = -1`), filling up the laptop disk.


* *Mitigation:* Explicitly configure `max_slot_wal_keep_size = 2GB` in `postgresql.conf` and monitor slot lag via `/monitor`.




* **RSK-002 (Shadow State / Pre-Image Storage Overhead):** Storing a shadow lookup of 10M orders to resolve `UPDATE` before-images can consume 1–2 GB of RAM/disk if unoptimized.


* *Mitigation:* Either enable `REPLICA IDENTITY FULL` on the `orders` table so PostgreSQL supplies the old tuple directly in the WAL stream, or store only compact fixed-width columns `(order_id -> tenant_id, created_ts, status_enum, amount_cents)` in an indexed local table.




* **RSK-003 (In-Flight Write False Mismatches During Correctness Testing):** Comparing the aggregation API against a direct database query while writes are actively flowing will report false mismatches due to timing differences between the two reads.


* *Mitigation:* Evaluate correctness at a consistent snapshot or compare the API's `as_of` LSN against a direct query bounded by that exact LSN/timestamp.





### 11.2 Open Questions & PRD Ambiguities

*(Identified explicitly for clarification with organizers upon receiving the generator script)*

* **AMB-001 (Physical Tenant Topology — Shared Table vs. Separate Schemas/DBs):** Section 1 states *"Each tenants' orders, products and customers sit in their respective database"*, whereas FR-5 specifies *"Tenant filter on every query"*. **Action:** Inspect the organizer's generator script immediately at kickoff to confirm whether tenants share tables via `tenant_id` or reside in separate schemas/databases.


* **AMB-002 (Sales Revenue Semantics Under Cancelled Orders):** When an order changes status from `placed` to `cancelled` (FR-6), should the total sales (`SUM`) in FR-1 and Top-10 product sales in FR-3 exclude `cancelled` orders, or count all orders regardless of status? **Action:** Align aggregation filter predicates strictly with the reference SQL queries used by the judges' correctness checker.


* **AMB-003 (Timestamp Alignment & Timezone Definition):** Does the correctness test issue `from`/`to` timestamps aligned to UTC day/hour boundaries, or arbitrary second/millisecond timestamps? **Action:** Support UTC ISO-8601 timestamps and implement hybrid range decomposition (interior buckets + exact edge slices) so both aligned and arbitrary ranges return 100% exact results.


* **AMB-004 (Order-to-Product Relationship Cardinality):** Does an `order` row reference a single `product_id` directly, or is there a child `order_items` join table? **Action:** Verify generator schema; if `order_items` exists, include it in the CDC publication to maintain `agg_products_daily`.



---

## 12. Requirements Traceability Matrix (RTM)

| Req ID | Requirement Summary | Authoritative Source | Priority | Measurable Verification Method |
| --- | --- | --- | --- | --- |
| **FR-001** | Asynchronous CDC ingestion without synchronous primary DB locks | PRD Sec 1, 2 (Goal 3) & Research Report Sec 5, 8

 | **MUST** | Code/config inspection (`wal_level=logical` / change streams) + OLTP benchmark (NFR-005).

 |
| **FR-002** | Atomic transactional commit of summary deltas and CDC LSN offset | Research Report Sec 4.3, 8 (Opportunity 1)

 | **SHOULD** | Kill CDC worker mid-stream, restart, and verify zero duplicate counting against ground truth.

 |
| **FR-003** | Bootstrap 1M (10 tenants) and 10M (100 tenants) organizer datasets | PRD Sec 4 (Data size)

 | **MUST** | Automated seed script execution verifying base and summary row totals.

 |
| **FR-004** | Pre-aggregated time-bucketed summary tables keyed by tenant | PRD Sec 1, 4 & Research Report Sec 2, 8

 | **MUST** | Schema inspection + query `EXPLAIN ANALYZE` verifying index-only bucket scans.

 |
| **FR-005** | Hybrid range decomposition for arbitrary non-aligned `from`/`to` windows | PRD Sec 2 (Goal 5), FR-7 & Research Report Sec 2, 7 (G2)

 | **MUST** | Execute correctness test with unaligned timestamps (e.g., `10:17:23` to `14:42:09`) vs direct SQL.

 |
| **FR-006** | Counts (`COUNT`) and total sales (`SUM`) for a time range | PRD Sec 3 (**FR-1**)

 | **MUST** | Automated API test on `GET /tenants/{id}/orders/count` comparing against direct DB query.

 |
| **FR-007** | Trend over time (orders/sales per hour or per day) | PRD Sec 3 (**FR-2**)

 | **MUST** | Automated API test on `GET /tenants/{id}/orders/trend` across 30-day window vs direct DB query.

 |
| **FR-008** | Top-$N$ list (Top 10 products by sales this week) | PRD Sec 3 (**FR-3**)

 | **MUST** | Automated API test on `GET /tenants/{id}/products/top?limit=10` vs direct DB query.

 |
| **FR-009** | Exact per-product bucket sums ranked at query time for Top-$N$ under deletes | Research Report Sec 2, 4.5, 7 (G7)

 | **SHOULD** | Cancel orders for the #1 ranked product and verify the #11 product promotes into Top-10 accurately.

 |
| **FR-010** | Breakdown by category (orders by status: `placed`, `shipped`, `cancelled`) | PRD Sec 3 (**FR-4**)

 | **MUST** | Automated API test on `GET /tenants/{id}/orders/by-status` vs direct DB `GROUP BY status`.

 |
| **FR-011** | Mandatory tenant filter on every query (`tenant_id` isolation) | PRD Sec 2 (Goal 4), Sec 3 (**FR-5**)

 | **MUST** | Negative and cross-tenant security test suite verifying zero data leakage between stores.

 |
| **FR-012** | Reject missing/invalid `tenant_id` requests with HTTP 400/403 | PRD Sec 3 (**FR-5**)

 | **MUST** | Automated HTTP integration test omitting or falsifying `tenant_id` parameter.

 |
| **FR-013** | Incremental reflection of newly inserted orders | PRD Sec 3 (**FR-6**)

 | **MUST** | Insert live order via script, poll API, verify count increases by $+1$ within $< 30\text{ s}$.

 |
| **FR-014** | Incremental reflection of order updates (`placed` $\rightarrow$ `cancelled`) | PRD Sec 3 (**FR-6**)

 | **MUST** | Update order status in primary DB, poll API, verify `placed` decrements by $1$ and `cancelled` increments by $1$.

 |
| **FR-015** | Before-image retrieval via `REPLICA IDENTITY FULL` / pre-images or shadow state | Research Report Sec 5, 7 (G1), 8

 | **MUST** | Verify update retraction accuracy across 1,000 random status/amount mutations.

 |
| **FR-016** | Simple REST API + endpoint and startup `README.md` | PRD Sec 3 (**FR-7**), Sec 4

 | **MUST** | Clean-room judge walkthrough following `README.md` from `git clone` to API query.

 |
| **FR-017** | Response includes data freshness watermark (`as_of`, `lag_ms`) | PRD Sec 3 (**Bonus B-1**) & Research Report Sec 8

 | **SHOULD** | Inspect JSON response payload on all endpoints for accurate `as_of` and `lag_seconds` fields.

 |
| **FR-018** | Per-tenant heavy query concurrency / rate limiting | PRD Sec 3 (**Bonus B-3**)

 | **SHOULD** | Flood API with 100 concurrent requests for Tenant 1; verify HTTP `429 Too Many Requests` while Tenant 2 succeeds.

 |
| **FR-019** | Tenant-fair DRR scheduling for ingestion/backfill queues | Research Report Sec 7 (G6), 8 (Opportunity 3)

 | **SHOULD** | Run backfill for largest tenant while inserting orders for small tenant; verify small tenant freshness $< 30\text{ s}$.

 |
| **FR-020** | Add a new metric by editing a declarative config file, not code | PRD Sec 3 (**Bonus B-4**) & Research Report Sec 8

 | **MAY** | Add a new metric definition to `metrics.yaml`, reload service, query new metric, and compare with SQL.

 |
| **FR-021** | Simple monitoring page showing query time and data lag | PRD Sec 3 (**Bonus B-5**)

 | **SHOULD** | Load `GET /monitor` in browser/curl and verify live p50/p95 query latency and CDC lag telemetry.

 |
| **FR-022** | Rebuild all numbers from scratch if something goes wrong | PRD Sec 3 (**Bonus B-2**)

 | **SHOULD** | Truncate summary table, trigger rebuild command, and verify 100% correctness restoration.

 |
| **FR-023** | Per-(tenant, day) hash digest verification and targeted DBLog-style bucket repair | Research Report Sec 7 (G4, G5), 8 (Opportunity 2)

 | **SHOULD** | Corrupt 5 random summary buckets via fault script, run `/verify`, and verify targeted repair reads $< 1\%$ of base rows.

 |
| **NFR-001** | Query speed at 10M records: $< 1\text{ s}$ (Good), $< 200\text{ ms}$ (Great) | PRD Sec 2 (Goal 1), Sec 4 (Targets)

 | **MUST** | Benchmark harness latency histogram over 10M records across 100 tenants.

 |
| **NFR-002** | Scaling 1M $\rightarrow$ 10M records: $< 5\text{x}$ slower (Good), $< 2\text{x}$ slower (Great) | PRD Sec 2 (Goal 6), Sec 4 (Targets)

 | **MUST** | Ratio comparison of benchmark suite p95 latencies between 1M and 10M runs.

 |
| **NFR-004** | Freshness lag: $< 5\text{ min}$ (Good), $< 30\text{ s}$ (Great) | PRD Sec 2 (Goal 2), Sec 4 (Targets)

 | **MUST** | End-to-end write-to-visibility timestamp delta test.

 |
| **NFR-005** | Primary DB safety: $< 25\%$ slower (Good), $< 10\%$ slower (Great) | PRD Sec 2 (Goal 3), Sec 4 (Targets)

 | **MUST** | `pgbench` / OLTP baseline latency vs. concurrent dashboard + CDC load test.

 |
| **NFR-007** | Correctness: Matches on most queries (Good), Matches on all queries (Great) | PRD Sec 2 (Goal 5), Sec 4 (Targets)

 | **MUST** | Automated snapshot-aligned comparison against direct database queries across 100 random ranges.

 |