# Multi-Tenant Live Rollups at Scale: Research Report for the Mitigata Aggregation-Layer PRD

## 1. Executive summary

The core technical problem in the PRD, keeping counts, sums, trends, breakdowns and top-N lists fresh over a changing OLTP table without re-scanning it, has been studied for a long time. The research name for it is incremental view maintenance (IVM). The algebra for distributive aggregates like COUNT and SUM is settled. An update is a retraction of the old row plus an insertion of the new row, and every group touched by the change gets adjusted by the difference. DBSP formalises this, Materialize and Feldera productise it, and PostgreSQL, ClickHouse, TimescaleDB, Snowflake and Databricks each ship a partial version.

Four things follow for a 48-hour team.

First, every query in FR-1 to FR-4 can be computed exactly from pre-aggregated buckets keyed by (tenant, time bucket, dimension). Approximate methods such as sketches or sampling aren't needed, and they would work against the "matches on all queries" target.

Second, the hard parts are engineering. You need the before-image of an updated row so you can retract it. You need arbitrary `from/to` ranges that don't line up with bucket edges to still give exact answers. You need to bootstrap and rebuild without a full-table scan that blocks the primary. And you need to prove correctness and freshness, not just claim them. The off-the-shelf tools I checked each leave at least one of these to you.

Third, the most defensible contribution isn't a new aggregation algorithm. It's a CDC-driven rollup engine that is correct under retractions, plus a measurable correctness and freshness contract. That means per-response freshness watermarks, per-(tenant, day) digest checks against the source, and targeted repair of only the buckets that disagree. Close prior art exists: Materialize's self-correcting sinks, DBLog's watermark-based chunked copying, and Cassandra-style anti-entropy. The specific combination for aggregate summaries in a laptop-scale multi-tenant setting appears less common. That is my judgement, not an established fact. Treat it as an engineering opportunity, not a research novelty claim.

Fourth, tenant skew is mostly removed at query time by pre-aggregation, because query cost depends on the number of buckets, not rows. Skew still shows up in ingest, backfill and rebuild, which is where the fairness bonus (B-3) can be shown in a meaningful way.

## 2. Technical interpretation of the PRD

Restated as a systems problem: keep a derived dataset V = Q(Orders, OrderItems, Products) up to date, where Q is a set of grouped aggregates, under inserts, updates and occasional deletes to the base tables. Do it with bounded extra load on the primary, read latency under 200 ms, and visibility lag under 30 seconds. Then show all of this with measurements.

**Query decomposition (my inference from the PRD examples):**

| PRD query | Aggregate type | Natural summary key | Incremental difficulty |
|---|---|---|---|
| FR-1 count + total sales in range | COUNT, SUM: distributive and invertible | (tenant, bucket) | Easy if retractions are handled |
| FR-2 orders per hour/day | COUNT per bucket | (tenant, hour) with days rolled up from hours | Easy |
| FR-3 top-10 products by sales this week | SUM per product, then ORDER BY/LIMIT | (tenant, day, product) | SUM is easy. A materialised top-k list is not self-maintainable under deletions (Yi et al., ICDE 2003), but re-ranking per-product sums at query time avoids that problem |
| FR-4 orders by status | COUNT per category | (tenant, bucket, status) | Easy, but status updates move a row between groups, so you need the old status |
| FR-6 updates (placed→cancelled) | Retraction + insertion | n/a | Needs the before-image or your own shadow state |

Three details in the PRD drive the architecture.

The first is correctness against a direct query for arbitrary `from`/`to`. If a user asks for 10:17 to 14:42, day or hour buckets alone give the wrong answer. You either restrict the API to aligned ranges, which is risky with judges, or you split the range into full buckets in the middle plus exact edge pieces. TimescaleDB's "real-time aggregates" mode solves a closely related problem by combining materialised buckets with the most recent raw data at query time. Its documentation also says that late-arriving historical data isn't reflected until the next refresh, which makes it "eventually consistent for historical data" [Source](https://www.tigerdata.com/docs/learn/continuous-aggregates/real-time-aggregates).

The second is "safe for the database", measured as the slowdown in normal app queries. This rules out designs that add synchronous work to the write path unless you measure the overhead. Trigger-based IVM is one example: pg_ivm's own README says base-table updates get slower because triggers update the view on every modification statement [Source](https://github.com/sraoss/pg_ivm).

The third is skew: "a few tenants are much bigger than the rest". Pre-aggregation removes most of the read-side skew. Ingest bursts, bootstrap and rebuild are still skewed.

One thing is still open. The organisers' data generator may target MongoDB or PostgreSQL. CDC mechanics differ a lot between them: logical decoding vs change streams, and how before-images are obtained. Section 9 covers both.

## 3. Research methodology and search coverage

I ran targeted searches on Google Scholar-indexed venues (PVLDB, SIGMOD, ICDE, OSDI, CIDR, PODS), arXiv, and official documentation and source repositories for every system named in the brief. I then read primary documents for the claims that matter most: the pg_ivm README, ClickHouse's incremental materialized view docs, TimescaleDB's real-time aggregates docs, Databricks' Enzyme paper, and Materialize's self-correction write-up.

Labels used in this report: **[PR]** peer-reviewed, **[IND]** peer-reviewed industry track or vendor-authored paper, **[PRE]** preprint or technical report, **[DOC]** vendor documentation or claim, **[INF]** my own technical inference.

Limits on coverage. I did not reproduce any benchmark. I did not exhaustively check every 2026 workshop paper. For several papers I verified existence, venue and abstract-level contribution but did not re-read the full experimental sections. In those cases I give no specific numbers, and you should read the paper before quoting results. Where I couldn't verify something, I say so.

## 4. Annotated bibliography

### 4.1 Incremental view maintenance and incremental computation

**Gupta & Mumick, "Maintenance of Materialized Views: Problems, Techniques, and Applications" (IEEE Data Engineering Bulletin, 1995) [PR, survey].** This is the standard survey of the problem space: immediate vs deferred maintenance, self-maintainability, and the counting and delta approaches [Source](https://www.researchgate.net/publication/2579074_Maintenance_of_Materialized_Views_Problems_Techniques_and_Applications). Relevance: it gives you the vocabulary you need. COUNT and SUM are self-maintainable given the delta. MIN, MAX and top-k are not under deletions. Limitation: single-node, pre-streaming, no multi-tenancy. No code.

**Budiu, McSherry, Ryzhyk, Tannen, "DBSP: Automatic Incremental View Maintenance for Rich Query Languages" (PVLDB 16, 2023; VLDB 2023 Best Paper) [PR].** This paper models database changes as streams of Z-sets, which are multisets with integer weights, and derives incremental versions of relational operators mechanically [Source](https://www.vldb.org/pvldb/vol16/p1601-budiu.pdf). An extended version also exists [Source](https://mihaibudiu.github.io/work/budiu-vldb25.pdf). Relevance: the Z-set view is exactly the right mental model for FR-6. "placed→cancelled" becomes (old_row, −1) plus (new_row, +1), and grouped SUM and COUNT apply the weights linearly. Code: MIT-licensed, now developed as Feldera [Source](https://github.com/feldera/feldera). Limitation for you: it's a general engine with its own runtime and memory footprint. It doesn't address tenant isolation or primary-DB load.

**McSherry, Murray, Isaacs, Isard, "Differential Dataflow" (CIDR 2013) [PR].** Incremental computation over partially ordered time, including iterative computation [Source](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper111.pdf). Relevance: this is the foundation of Materialize. The `(data, time, diff)` update model is directly reusable in your change log. Code: open-source Rust library (not checked in this session).

**Ahmad, Kennedy, Koch, Nikolic, "DBToaster: Higher-order Delta Processing for Dynamic, Frequently Fresh Views" (PVLDB 5, 2012; extended in VLDB Journal 2014) [PR].** Recursive "viewlet transforms" that materialise higher-order deltas, so maintenance work becomes fine-grained, low-cost updates [Source](http://vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf). Relevance: it shows that maintaining extra auxiliary views (for example, per-product sums to support top-N) is the standard way to make otherwise expensive views cheap. Limitation: in-memory main-memory setting, compiled code, not multi-tenant.

**Gjengset et al., "Noria: dynamic, partially-stateful data-flow for high-performance web applications" (OSDI 2018) [PR].** Introduces partially-stateful dataflow, where state can be evicted and rebuilt on demand, and positions it as a replacement for the "database + cache" two-tier pattern [Source](https://www.usenix.org/conference/osdi18/presentation/gjengset). Relevance: very close to your problem framing, since it targets read-heavy web backends that want cache-speed reads of derived data. Partial state is relevant if memory gets tight with many tenants. Limitation: it's a research prototype and is no longer actively maintained as far as I know (not verified in this session).

**Olteanu, "Recent Increments in Incremental View Maintenance" (arXiv 2024; PODS-community overview) [PRE/overview].** Covers fine-grained complexity results for IVM [Source](https://arxiv.org/abs/2404.17679). Relevance: theoretical. It confirms that the aggregate-only, join-light views in this PRD sit in the "easy" part of the complexity landscape. The hard cases are cyclic joins.

**Zaidi et al. (Databricks), "Enzyme: Incremental View Maintenance for Data Engineering" (SIGMOD 2026 companion/industry; arXiv 2603.27775) [IND].** A cost-based IVM engine for Spark Declarative Pipelines. It chooses between incremental and full refresh per run, decomposes AVG into SUM and COUNT, and uses change data feeds with +1/−1 change types [Source](https://arxiv.org/html/2603.27775v1). Two points matter for you. The paper notes that streaming IVM minimises latency, while batching amortises cost. It also notes that much prior IVM research depends on efficient secondary indexes, which lakehouses lack. Relevance: it validates the "choose between incremental and full refresh" decision as a first-class problem. Limitation: closed source. The results are production claims from the vendor.

**Sotolongo et al. (Snowflake), "Streaming Democratized: Ease Across the Latency Spectrum with Delayed View Semantics and Snowflake Dynamic Tables" (SIGMOD 2025 companion; arXiv 2504.10438) [IND].** Defines delayed view semantics: a view is allowed to equal the query evaluated at some earlier point in time within a target lag [Source](https://arxiv.org/abs/2504.10438). Relevance: this is the formal statement of what your freshness bonus B-1 should report ("the result is exact as of time t, which is 20 s ago"). Limitation: closed source, cloud-only.

**Battiston, Dumitrescu et al., "OpenIVM: a SQL-to-SQL Compiler for Incremental Computations" (demo, arXiv 2404.16486) [PRE/demo].** Compiles materialized view definitions into incremental SQL that runs on DuckDB (and Spark) [Source](https://arxiv.org/html/2404.16486v1). Code: open source [Source](https://github.com/ila/openivm). Relevance: the closest prior art to the "add a metric via config" bonus (B-4). It compiles a declarative definition into delta-maintenance SQL. I didn't verify the exact author list in this session. Check the arXiv page before citing authors.

### 4.2 Production real-time analytics architectures

**Gupta et al. (Google), "Mesa: Geo-Replicated, Near Real-Time, Scalable Data Warehousing" (PVLDB 7, 2014) [IND].** Stores pre-aggregated measurement data with versioned delta batches that are compacted over time, and serves consistent, repeatable answers at low latency [Source](https://research.google/pubs/mesa-geo-replicated-near-real-time-scalable-data-warehousing/). Relevance: the "versioned deltas + periodic compaction + aggregate-key tables" pattern maps very closely to a hackathon-scale design. Mesa is effectively an ads-scale version of your summary table. Limitation: proprietary, planet-scale assumptions.

**Agiwal et al. (Google), "Napa: Powering Scalable Data Warehousing with Robust Query Performance at Google" (PVLDB 14, 2021) [IND].** Makes the trade-off between query performance, freshness and cost explicit and configurable through a "Queryable Timestamp" [Source](http://www.vldb.org/pvldb/vol14/p2986-sankaranarayanan.pdf). Relevance: the Queryable Timestamp is a production version of a freshness watermark. Your API can expose the same concept. Limitation: proprietary.

**Yang et al., "Druid: a real-time analytical data store" (SIGMOD 2014) [IND].** Column-oriented, immutable but versioned segments, real-time ingestion nodes plus historical nodes [Source](https://dl.acm.org/doi/10.1145/2588555.2595631). Relevance: the split between a real-time tail and an immutable historical store is the same split you need for exact edge ranges. Limitation: segments are immutable, so row updates require re-indexing segments. That's awkward for FR-6.

**Im et al. (LinkedIn), "Pinot: Realtime OLAP for 530 Million Users" (SIGMOD 2018) [IND].** Serves very high query concurrency with near-real-time ingestion [Source](https://dl.acm.org/doi/10.1145/3183713.3190661). Relevance: user-facing analytics at high QPS is the same product shape as tenant dashboards. Upserts were added later (see Section 5).

**Schulze, Schreiber, Yatsishin, Dahimene, Milovidov, "ClickHouse – Lightning Fast Analytics for Everyone" (PVLDB 17, 2024) [IND].** Describes the MergeTree family, background merges and incremental materialized views [Source](https://www.vldb.org/pvldb/vol17/p3731-schulze.pdf). Relevance: covered in detail in Section 5.

**Chandramouli et al., "Trill: A High-Performance Incremental Query Processor for Diverse Analytics" (PVLDB 8, 2014) [IND, Microsoft].** A single incremental engine covering real-time, offline and progressive analytics [Source](https://dl.acm.org/doi/10.14778/2735496.2735503). Relevance: shows that streaming and batch over the same temporal model can share one engine. This is useful for the rebuild path (B-2), which can be "replay the stream from scratch".

### 4.3 CDC, consistency, exactly-once processing

**Andreakis & Papapanagiotou (Netflix), "DBLog: A Watermark Based Change-Data-Capture Framework" (arXiv 2010.12597, 2020) [PRE].** Interleaves chunked `SELECT`s of table rows with log events using low and high watermarks. This makes it possible to take full or partial snapshots without locks while the log keeps streaming [Source](https://arxiv.org/abs/2010.12597). Follow-up work in 2026 formalises the protocol: "A Theoretical Study of DBLog" [Source](https://arxiv.org/abs/2605.31475), and "Generalized DBLog: A Verified Contract for Interleaving Database Rows with a Change Log", whose abstract states that the log wins when it races the copy and that Debezium and Flink CDC have adopted the design [Source](https://arxiv.org/abs/2609.08160). Both are preprints. Relevance: this is the key paper for bootstrap and for "rebuild without blocking the primary" (B-2). It also lets you rebuild a single tenant or a single day.

**Ananthanarayanan et al. (Google), "Photon: Fault-tolerant and Scalable Joining of Continuous Data Streams" (SIGMOD 2013) [IND].** Exactly-once joining of continuously flowing streams that have no guaranteed global order [Source](https://research.google.com/pubs/archive/41318.pdf). Relevance: shows that exactly-once at scale comes from idempotent, ID-keyed state, not from transport guarantees. Your consumer should dedupe by (order_id, LSN or version).

**Kraft et al., "Epoxy: ACID Transactions Across Diverse Data Stores" (PVLDB 16, 2023) [PR].** Uses Postgres as a transaction coordinator for secondary stores such as Elasticsearch and MongoDB [Source](https://dl.acm.org/doi/pdf/10.14778/3611479.3611484). Relevance: important if you want the summary store and the CDC offset to commit atomically. The cheap version of this idea is to store the replication LSN in the same transaction as the summary deltas.

### 4.4 Multi-tenancy and workload isolation

**Aulbach, Grust, Jacobs, Kemper, Rittinger, "Multi-Tenant Databases for Software as a Service: Schema-Mapping Techniques" (SIGMOD 2008) [PR].** Covers shared-table schema mapping and introduces "Chunk Folding" [Source](https://dl.acm.org/doi/10.1145/1376616.1376736). Relevance: the PRD wording is ambiguous ("each tenant's data in their respective database"). Shared tables with a `tenant_id` column are the most likely generator layout and the easiest to aggregate.

**Narasayya et al. (Microsoft), "SQLVM: Performance Isolation in Multi-Tenant Relational Database-as-a-Service" (CIDR 2013) [PR].** Reserves CPU, I/O and memory per tenant inside one DB process, with metering to check that the promise holds [Source](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper25.pdf). The related "CPU sharing techniques for performance isolation" paper appeared in PVLDB 2014 [Source](https://dl.acm.org/doi/abs/10.14778/2732219.2732223). Relevance: the conceptual basis for B-3. Note that SQLVM isolates the *database engine*. Your layer isolates the *aggregation service*, which is a different problem.

### 4.5 Top-K, sketches and approximation

**Yi, Yu, Yang, Xia, Chen, "Efficient Maintenance of Materialized Top-k Views" (ICDE 2003) [PR].** Shows that a top-k view isn't self-maintainable under deletions unless it stores the whole base table. Proposes maintaining a top-k′ buffer with k′ ≥ k, refilled when needed [Source](https://ieeexplore.ieee.org/document/1260792/). Relevance: if you materialise "top-10 this week", cancellations can push an item out, and you then need the 11th item. The cheap fix [INF] is to keep exact per-(tenant, day, product) sums and rank at query time. With a realistic product catalogue per tenant this is small.

**Mouratidis, Bakiras, Papadias, "Continuous Monitoring of Top-k Queries over Sliding Windows" (SIGMOD 2006) [PR].** Grid-based techniques for top-k over sliding windows [Source](https://dl.acm.org/doi/10.1145/1142473.1142544). Relevance: "this week" is a sliding window. Background reading only, since the bucket approach makes this unnecessary at your scale.

**Metwally, Agrawal, El Abbadi, "Efficient Computation of Frequent and Top-k Elements in Data Streams" (ICDT 2005) [PR].** The Space-Saving algorithm [Source](https://link.springer.com/chapter/10.1007/978-3-540-30570-5_27). The SpaceSaving± line extends it to streams with bounded deletions and explains why the original variant only works when insertions and deletions aren't interleaved [Source](https://arxiv.org/abs/2309.12623). Relevance: this is the evidence *against* using approximate top-k here. Order cancellations are interleaved deletions, and the PRD wants exact matches.

**Agarwal, Cormode, Huang, Phillips, Wei, Yi, "Mergeable Summaries" (PODS 2012; TODS 2013) [PR]** [Source](https://dl.acm.org/doi/abs/10.1145/2213556.2213562), and **Flajolet et al., "HyperLogLog" (AofA 2007)** [Source](https://algo.inria.fr/flajolet/Publications/FlFuGaMe07.pdf). Relevance: needed only if you add distinct-customer metrics. Those aren't required by the PRD.

**Chaudhuri, Ding, Kandula, "Approximate Query Processing: No Silver Bullet" (SIGMOD 2017) [PR].** Reflects on why AQP has had limited adoption in practice [Source](https://dl.acm.org/doi/10.1145/3035918.3056097). Relevance: supports leaving AQP out of this PRD.

**Gray et al., "Data Cube: A Relational Aggregation Operator Generalizing Group-By, Cross-Tab, and Sub-Totals" (DMKD 1997) [PR].** Defines the CUBE operator and the classification of aggregates as distributive, algebraic or holistic [Source](https://link.springer.com/article/10.1023/A:1009726021843). Relevance: that classification tells you which metrics a config-driven engine (B-4) can accept. Distributive (count, sum) and algebraic (avg as sum/count) aggregates are fine. Holistic ones (median, exact distinct) need different handling.

## 5. Existing solutions and technology landscape

**PostgreSQL native materialized views.** `REFRESH MATERIALIZED VIEW` completely replaces the contents of the view [Source](https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html). Freshness is therefore bounded by how long a refresh takes. The full recompute runs on whichever instance holds the view, so if that is the primary it adds exactly the load the PRD wants to avoid. Not suitable on its own.

**pg_ivm (PostgreSQL License).** Immediate maintenance through AFTER triggers in the same transaction. Supports count, sum, avg, min and max. It doesn't support HAVING, ORDER BY/LIMIT, window functions or partitioned base tables. It also doesn't maintain views on logical-replication subscribers. The README shows base-table updates getting slower (for example 9 ms to 15 ms in its pgbench example), and maintenance needs an `ExclusiveLock` on the view for grouped views under concurrent writes. The README recommends against immediate IVM when base tables are modified frequently, and points out that min/max may need recomputation from base tables when the current extreme is deleted [Source](https://github.com/sraoss/pg_ivm). Assessment [INF]: in a multi-tenant shop, the exclusive lock on a GROUP BY view serialises concurrent order writes that touch the same view. That works directly against "database safety". It's a useful *baseline* to measure against, not a recommended design.

**PostgreSQL logical replication / logical decoding.** Publications can be filtered by row [Source](https://www.postgresql.org/docs/current/logical-replication-row-filter.html). By default the replica identity is the primary key, so UPDATE/DELETE events carry only the key of the old row. `REPLICA IDENTITY FULL` sends the whole old row [Source](https://www.postgresql.org/docs/current/logical-replication-publication.html), with documented performance costs [Source](https://xata.io/blog/replica-identity-full-performance). There's also an operational risk. A stalled slot keeps WAL indefinitely when `max_slot_wal_keep_size` is −1, which is the default [Source](https://www.postgresql.org/docs/current/runtime-config-replication.html). Implication [INF]: for FR-6 you either enable REPLICA IDENTITY FULL on `orders` (simple, costs extra WAL) or keep a shadow copy of the fields you aggregate on, keyed by order_id, in the aggregation layer. That's the stateful-operator approach used by Flink, Materialize and Feldera.

**Debezium.** Supports incremental snapshots based on DBLog, triggered by signals, so snapshots can run chunk by chunk without long locks [Source](https://debezium.io/documentation/reference/stable/connectors/postgresql.html) [Source](https://debezium.io/blog/2021/10/07/incremental-snapshots/). The MongoDB connector uses change streams and needs a replica set or sharded cluster [Source](https://debezium.io/documentation/reference/stable/connectors/mongodb.html). Laptop fit: workable, but Kafka Connect plus a broker adds weight. Redpanda or a direct logical-decoding client in Python or Go is lighter.

**MongoDB change streams and aggregation.** Change streams resume from a resume token, and you must reuse the same pipeline [Source](https://www.mongodb.com/docs/manual/changestreams/). Pre-images (`fullDocumentBeforeChange`) need `changeStreamPreAndPostImages` enabled on the collection (MongoDB 6.0+) [Source](https://www.mongodb.com/docs/manual/changestreams/). On-demand materialized views use `$merge`/`$out` and are refreshed by re-running the pipeline [Source](https://www.mongodb.com/docs/manual/core/materialized-views/). Assessment: change streams plus pre-images give you retraction-capable CDC. `$merge` alone is periodic recompute, not true IVM.

**ClickHouse (Apache 2.0).** Incremental materialized views behave like insert triggers that run on each inserted block and write partial aggregate states to a SummingMergeTree or AggregatingMergeTree target. Merges happen asynchronously in the background, so queries must use `FINAL` or re-aggregate. Only inserts into the left-most table fire the view. Updates to joined tables don't propagate [Source](https://clickhouse.com/docs/concepts/features/materialized-views/incremental-materialized-view). Updates and cancellations have to be modelled with sign/version columns (Collapsing or VersionedCollapsingMergeTree), and queries must aggregate with the sign [Source](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/versionedcollapsingmergetree). Workload scheduling can share CPU and I/O between workloads [Source](https://clickhouse.com/docs/concepts/features/configuration/server-config/workload-scheduling). Assessment [INF]: an excellent query engine. The retraction logic still has to come from your CDC consumer, which must emit (−1 old, +1 new) rows. ClickHouse doesn't derive them.

**TimescaleDB continuous aggregates.** Background-refreshed, time-bucketed materialisations. They can optionally combine with raw recent data ("real-time aggregates", disabled by default since v2.13). Changes to already-materialised history only appear after a refresh [Source](https://www.tigerdata.com/docs/learn/continuous-aggregates/real-time-aggregates). Hierarchical aggregates (aggregates of aggregates) are supported [Source](https://www.tigerdata.com/docs/learn/continuous-aggregates/hierarchical-continuous-aggregates). Assessment: a very good fit for time bucketing. But a status update on an old order is exactly the "historical modification" case. Freshness then depends on the refresh policy, and refreshes run on the same instance as the primary, unless you replicate into a separate Timescale instance.

**Materialize (BSL; free self-managed Community edition with a license key, memory-capped).** Differential-dataflow IVM with strict-serializable reads by default [Source](https://materialize.com/docs/serve-results/isolation-level/) [Source](https://materialize.com/self-managed/community-license/). Its self-correcting materialized view sinks continuously diff the persisted output against the dataflow output to prevent drift, at the cost of a memory spike during hydration [Source](https://materialize.com/blog/self-correcting-materialized-views/). Assessment: the strongest correctness story among off-the-shelf tools. Licensing and memory make it a riskier laptop demo.

**RisingWave (Apache 2.0).** Streaming SQL with a native Postgres CDC source and incrementally maintained materialized views. A standalone mode is available [Source](https://docs.risingwave.com/ingestion/sources/postgresql/pg-cdc) [Source](https://github.com/risingwavelabs/risingwave). Assessment: the fastest path to a "real IVM engine" demo. The trade-off is that your contribution then shrinks to configuration.

**Feldera (DBSP; MIT).** Incremental SQL engine built on DBSP [Source](https://github.com/feldera/feldera). Same trade-off as RisingWave.

**Apache Pinot / Druid.** Pinot supports full and partial upsert on real-time tables [Source](https://docs.pinot.apache.org/build-with-pinot/ingestion/upsert-dedup/upsert), and tenants are first-class at the cluster level, with each table assigned to server and broker tenants [Source](https://docs.pinot.apache.org/architecture-and-concepts/components/cluster/tenant). Druid offers query priority and laning for mixed workloads [Source](https://druid.apache.org/docs/latest/operations/mixed-workloads/) and documents its multitenancy considerations [Source](https://druid.apache.org/docs/latest/querying/multitenancy/). Assessment: both are heavy for a laptop (multiple JVM services plus ZooKeeper in typical setups). Tenancy in these systems usually means *clusters of servers*, not row-level end-customer isolation.

**DuckDB.** Can read Postgres tables directly through ATTACH [Source](https://duckdb.org/docs/2.0/core_extensions/postgres/overview.html). OpenIVM adds IVM [Source](https://github.com/ila/openivm). Assessment: excellent as the correctness *oracle* and for one-off rebuilds against a snapshot. Its single-writer model needs care if ingest and the API share one database.

**Redis.** Sorted sets with `ZINCRBY` give O(log N) ranked updates [Source](https://redis.io/docs/latest/develop/use-cases/leaderboard/). Fine as a hot cache. Using it as the system of record for aggregates makes recovery and verification harder [INF].

**Snowflake Dynamic Tables / Databricks Enzyme / BigQuery.** These provide target-lag or scheduled incremental refresh in the cloud ([DVS paper](https://arxiv.org/abs/2504.10438), [Enzyme paper](https://arxiv.org/html/2603.27775v1)). They're useful as conceptual references for freshness semantics and refresh-strategy selection. They are out of scope for a laptop. I didn't investigate BigQuery materialized views in this session.

**Row-level tenant security.** PostgreSQL RLS policies can enforce tenant filters inside the database [Source](https://www.postgresql.org/docs/current/ddl-rowsecurity.html). pg_ivm notes that policy changes made after an IMMV is created aren't applied until the view is refreshed [Source](https://github.com/sraoss/pg_ivm). That's a subtle isolation pitfall for any derived store.

## 6. Comparative matrix

Ratings are my own qualitative assessment for *this PRD on a laptop* [INF]. They aren't benchmark results. Vendor latency claims come from different workloads and can't be compared directly.

| Approach | Query latency | Freshness | Primary DB impact | Correctness under updates/deletes | Tenant isolation | Skew/scale | Recovery/rebuild | Laptop/48h fit |
|---|---|---|---|---|---|---|---|---|
| Direct indexed query on primary | Degrades with big tenants | Perfect | High (the problem) | Exact | Via WHERE/RLS | Poor | n/a | Trivial (baseline) |
| PG MV + periodic REFRESH | Fast | Refresh interval (≥ refresh duration) | Recompute spikes | Exact at refresh | Must add tenant key | Refresh cost grows with data | Full refresh only | Easy |
| pg_ivm (triggers) | Fast | Immediate | Synchronous write overhead + view locks | Exact for count/sum; min/max may recompute | Same DB | Lock contention on hot groups | `refresh_immv` (full) | Easy, risky on "DB safety" |
| Timescale continuous aggregates | Fast | Policy-bound for historical updates | Refresh on same instance unless replicated | Exact after refresh | Tenant column | Good for time series | Range refresh | Medium |
| CDC → custom summary tables (PG/SQLite/DuckDB) | Fast (bucket count) | Seconds | Low (WAL read) + slot risk | Exact if retractions implemented | Enforced in API + key prefix | Good read-side; skewed ingest | You build it (DBLog-style) | Medium, full control |
| CDC → ClickHouse Summing/Collapsing MT | Very fast | Seconds | Low | Exact if sign rows emitted; merge-time collapse | Key prefix; workload scheduler | Excellent | Re-insert from snapshot | Medium |
| CDC → RisingWave/Feldera | Fast | Sub-second to seconds (vendor docs) | Low | Engine-derived retractions | Must model in SQL | Good | Engine-specific | Medium; less own contribution |
| Materialize | Fast | Sub-second (vendor) | Low | Strong (strict serializable, self-correcting) | SQL-level | Memory-bound | Hydration/self-correction | License key + memory cap |
| Pinot/Druid | Very fast | Seconds | Low | Upsert (Pinot); re-index (Druid) | Cluster-level tenants, laning | Excellent | Segment rebuild | Heavy |

Summary: no single system satisfies all six PRD goals without you building something. The ones closest out of the box are the streaming IVM engines (RisingWave, Feldera, Materialize). They leave tenant fairness, a correctness *proof*, and the API freshness contract to you. The custom-CDC-plus-summary-table route needs the most code but gives you the most control over every judged metric.

## 7. Identified gaps

I've split these into **(A) established limitations** documented in sources, **(B) engineering opportunities**, and **(C) speculative ideas**.

**G1 (A). Retractions need before-images, and the defaults don't provide them.** Postgres sends only the old key by default. Mongo pre-images are opt-in. ClickHouse MVs fire only on inserts and can't derive retractions themselves (Sections 5 and 4.3 sources). Why: log-based CDC was designed for replication, where the key is enough. This is an implementation limitation, not a fundamental one. PRD impact: FR-6 fails silently. The "placed" count never goes down when an order is cancelled. Already addressed by stateful stream engines, which keep the old row in operator state. A 48-hour prototype can do this credibly: a shadow table keyed by order_id holding (tenant, created_at bucket, status, amount, product lines).

**G2 (A/B). Arbitrary time ranges vs pre-aggregated buckets.** Bucketed rollups are exact only for aligned ranges. Timescale's real-time mode combines materialised data with the raw tail, but historical edits lag [Source](https://www.tigerdata.com/docs/learn/continuous-aggregates/real-time-aggregates). This is a fundamental granularity trade-off. PRD impact: the correctness checker may well use timestamps that aren't aligned to buckets. Fix: decompose the range as day buckets for the interior, hour or minute buckets near the edges, and an optional raw edge query against the `(tenant_id, created_at)` index on a replica or snapshot. Edge scans cover at most one partial bucket on each side. Demonstrable in 48 h.

**G3 (A). Synchronous IVM conflicts with primary-DB safety.** pg_ivm documents write overhead and exclusive locks [Source](https://github.com/sraoss/pg_ivm). This is a fundamental trade-off: immediate consistency inside the transaction vs isolation from the write path. Asynchronous CDC moves the cost off the write path but brings in lag and replication-slot risk. Addressed in industry by every CDC-based system. Your contribution is to *measure* it: the PRD's 10%/25% slowdown test with pg_ivm vs CDC.

**G4 (A). Bootstrap and rebuild without long scans or locks.** Full `REFRESH` replaces everything. Initial CDC snapshots can be long. DBLog and Debezium incremental snapshots solve chunked, lock-free copying [Source](https://arxiv.org/abs/2010.12597). What I didn't find off the shelf, in the sources I reviewed, is *aggregate-aware partial repair*: rebuild only (tenant 42, 2026-10-12) once you know that bucket is wrong. Materialize's self-correction compares desired output against persisted output, but it works over the whole view and pays a hydration memory spike [Source](https://materialize.com/blog/self-correcting-materialized-views/). This is an engineering opportunity (B), and it maps directly to B-2.

**G5 (B). Verifiable correctness, not just claimed correctness.** The PRD scores "matches direct query". Most systems treat correctness as an internal property and expose no per-query evidence. A cheap verification is per-(tenant, day) digests: count, sum(amount), and sum of a hash of (order_id, status, amount), computed on both the source (on a replica, index-backed, rate-limited) and the summary side. This is anti-entropy applied to aggregates. Prior art includes Cassandra/Dynamo Merkle repair for replicas and Materialize self-correction. I didn't find a reference that applies digest-based reconciliation to multi-tenant rollups the way described here, but that's absence of evidence within my search, not proof that nothing exists.

**G6 (A/B). Tenant skew moves from query time to maintenance time.** With bucketed rollups, a 2M-order tenant and a 10k-order tenant cost about the same to *query*. Ingest bursts, bootstrap and repair still scale with tenant size. SQLVM-style reservations [Source](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper25.pdf) and Druid query laning [Source](https://druid.apache.org/docs/latest/operations/mixed-workloads/) solve this for *queries* inside an engine. Fair scheduling of *maintenance work* per tenant in an aggregation layer is less often documented. Opportunity (B). PRD impact: a big tenant's backfill shouldn't push small tenants' freshness past 30 seconds.

**G7 (A). Top-k under deletions.** Not self-maintainable (Yi et al.), and approximate sketches break with interleaved deletions (SpaceSaving±). This is a fundamental property. For this PRD it's mostly *dissolved* by keeping exact per-product sums and ranking at query time. It would only become a real problem if per-tenant product cardinality were huge, for example millions of SKUs, which you should check against the organisers' generator.

**G8 (B). Declarative metrics within incrementalisability limits.** pg_ivm restricts which aggregates are allowed. OpenIVM compiles SQL into delta SQL. Enzyme picks incremental vs full refresh by cost. A config-driven metric system (B-4) should *reject or downgrade* metrics that can't be maintained incrementally. That follows Gray's distributive/algebraic/holistic classification. Opportunity (B), not novel research.

**G9 (C). Adaptive per-tenant freshness/cost tuning**, i.e. "target lag" per tenant in the spirit of Snowflake's delayed view semantics. This is speculative for a 48-hour build. The benefit only shows up under heavy, sustained load, which the PRD workload may not create.

## 8. Ranked hackathon opportunities

### Opportunity 1 (highest priority): retraction-correct CDC rollups with exact range decomposition and a freshness watermark

- **Builds on:** DBSP Z-set semantics, Mesa delta tables, Timescale real-time aggregates, Napa's Queryable Timestamp.
- **Addresses:** G1, G2, G3, plus B-1.
- **Design:** The CDC consumer turns each change into weighted deltas: insert → +1, delete → −1, update → −1 for the old row plus +1 for the new row, with the old row taken from the shadow state. It upserts these into `agg_hour(tenant, hour, status, count, sum)` and `agg_product_day(tenant, day, product, qty, revenue)`. The replication LSN or resume token is committed in the same transaction as the deltas, so redelivered events are idempotent (Epoxy/Photon reasoning). The API splits every range into aligned buckets plus exact edges. Every response returns `as_of` (the commit timestamp of the last applied LSN) and `lag_ms`.
- **Why it's different:** this is honestly *not* a new idea. It's a careful, measured implementation of established ideas. What makes it competitive is exactness for any range, measured freshness, and measured primary-DB impact.
- **Hypothesis:** at 10M rows / 100 tenants, p95 query latency stays under 200 ms, p95 insert-to-visible stays under 5 s, and OLTP p95 latency degrades by less than 10% under dashboard load, compared with direct queries.
- **MVP:** Postgres primary, a separate Postgres or SQLite summary store, a Python or Go logical-decoding client using pgoutput or wal2json, and a FastAPI/Go HTTP layer.
- **Risks:** WAL retention if the consumer stalls (mitigate by setting `max_slot_wal_keep_size`), how big the shadow state gets, and the time-zone semantics of "per day".
- **Benchmark:** the PRD's own matrix (Section 9.2).

### Opportunity 2: verifiable aggregates with targeted, DBLog-style repair

- **Builds on:** DBLog/Generalized DBLog, Materialize self-correction, anti-entropy repair.
- **Addresses:** G4, G5, plus B-2 and B-5.
- **Design:** For each (tenant, day), keep a digest of (count, sum, XOR or sum of hash(order_id‖status‖amount)), updated incrementally alongside the rollups. A low-priority verifier recomputes the same digests on the source, on a replica or within a throttled read budget. On a mismatch it rebuilds only that bucket. It does so with a watermark-bracketed chunked select so concurrent changes aren't lost or double-counted (the "log wins" rule from Generalized DBLog). `/verify` and `/monitor` endpoints show mismatch counts and repair times.
- **Why it's different:** Materialize's self-correction is engine-internal and works over whole views. DBLog is a copying protocol, not an aggregate verifier. Combining them into per-bucket *verifiable* rollups is, as far as my search went, not a standard feature of the tools reviewed. Keep the claim modest: "applies established anti-entropy and watermark techniques to aggregate rollups".
- **Hypothesis:** after injecting k corrupted buckets, the system detects and repairs all of them while reading less than X% of the rows a full rebuild would read. X is roughly the fraction of affected tenant-days. Primary-DB p95 impact stays within the PRD's "great" threshold during repair.
- **MVP:** a nightly-style verifier loop plus a fault-injection script that corrupts random summary rows or drops CDC events.
- **Risks:** digests must be order-independent and must handle retractions (sum-of-hashes modulo 2^64 works, XOR does not handle multiplicities). Race conditions during repair need the watermark protocol.
- **Benchmark:** a detection/repair-time curve and rows read vs a full rebuild.

### Opportunity 3: tenant-fair maintenance and query admission under skew

- **Builds on:** SQLVM reservations, Druid laning, ClickHouse workload scheduling, weighted fair queueing.
- **Addresses:** G6, plus B-3.
- **Design:** Per-tenant token buckets on the API, with a query cost estimate equal to the number of buckets touched. Per-tenant queues for CDC apply and backfill, drained with deficit round robin, so a 3M-row backfill for tenant 7 can't starve tenant 42's live updates.
- **Hypothesis:** while the largest tenant is bootstrapping, small tenants' p95 freshness stays under 30 s with fair scheduling, vs above X s with FIFO.
- **MVP:** two queue implementations behind a flag, plus a skewed load generator.
- **Risks:** per-tenant ordering must be preserved (partition by tenant_id). Cross-tenant fairness doesn't conflict with correctness, because the aggregates are tenant-partitioned.
- **Benchmark:** per-tenant freshness distributions, FIFO vs DRR.

### Opportunity 4: config-defined metrics with an incrementalisability checker

- **Builds on:** OpenIVM, pg_ivm restrictions, Gray's aggregate classification, Enzyme's choice between incremental and full refresh.
- **Addresses:** G8, plus B-4.
- **Design:** A YAML file declares a metric (measure, aggregate, dimensions, grain). A compiler maps distributive and algebraic aggregates to delta rules and summary tables. Holistic or min/max metrics get flagged as "periodic recompute" or rejected.
- **Hypothesis:** a new metric goes live with a backfill using only a config change, and matches the direct query.
- **Risks:** scope creep. Keep it to count, sum, avg and filters.

### Opportunity 5 (lowest priority): exact top-N with a k′ candidate buffer

Yi et al.'s top-k′ approach only pays off if products per tenant are very many. Check the generator first. If the catalogue is modest, rank exact per-product sums at query time and skip this.

## 9. Recommended direction and supporting evidence

### 9.1 Recommendation

Build Opportunity 1 as the backbone, and add Opportunity 2 as the differentiating feature. Add Opportunity 3 if time allows, since it maps directly to bonus points. Don't adopt a full streaming IVM engine as the core. It would make your own contribution almost invisible, and it still wouldn't give you verification, a freshness contract, or tenant fairness. Do keep RisingWave or pg_ivm as a *comparison baseline* in your benchmark slide. Showing "we measured the alternative" is persuasive to judges.

**Suggested laptop stack:**

- **Primary database:** PostgreSQL with `wal_level=logical` and `max_slot_wal_keep_size` set.
- **CDC consumer:** a single process using pgoutput (psycopg or pglogrepl in Go).
- **Summary store:** a separate Postgres container with CPU limits, so it can't steal primary CPU during the safety test.
- **Cache:** an optional Redis cache for the hot "today" view.
- **Oracle:** DuckDB attached to a snapshot or replica, for the correctness comparison.
- **Packaging:** Docker Compose.

If the organisers' generator is MongoDB, swap in change streams with `changeStreamPreAndPostImages` enabled on `orders` and store resume tokens transactionally with the deltas.

Rough sizing [INF, check against the generator]: 100 tenants × 365 days × 24 hours × ~4 statuses ≈ 3.5M hourly rows at most, and usually far fewer, because many buckets are empty. A 30-day trend reads at most 30 day-rows per tenant, and "last 7 days" reads about 7 day-rows plus 2 edges. Query cost doesn't depend on whether the tenant has 10k or 3M orders. That property is what gives you the "1M → 10M growth less than 2x" result.

### 9.2 Benchmark plan, mapped to the PRD

| PRD metric | Experiment |
|---|---|
| Query speed | p50/p95/p99 per endpoint at 1M/10M rows, for small, median and largest tenants, cold and warm |
| Freshness | Insert or update an order with a client timestamp, poll the API until it appears; report the distribution and `lag_ms` self-reports |
| Database safety | pgbench-style OLTP mix (or replayed generator writes) alone vs with N concurrent dashboard clients; compare against direct-query and pg_ivm baselines |
| Correctness | For random tenants and random non-aligned ranges, compare the API against direct SQL on a snapshot at the same LSN; report exact-match % (target 100%) |
| Growth | Same suite at 1M and 10M; report the ratio |
| Bonus evidence | Fault injection → repair time (Opportunity 2); per-tenant freshness under a big-tenant backfill (Opportunity 3) |

One methodological point: compare against direct queries *at the same snapshot or LSN*. Otherwise in-flight writes will create false mismatches.

### 9.3 Fact vs inference

The following are established by the cited sources: the behaviour of pg_ivm, ClickHouse MVs, Timescale real-time aggregates, Postgres replica identity, Mongo pre-images, Materialize self-correction, DBLog watermarks, and the top-k self-maintainability result.

The following are my inferences and need validation in your own benchmarks: the latency and size estimates, the claim that the custom CDC route best satisfies all six goals, and the view that digest-based per-bucket repair is underexplored in available tools.

The papers above report experimental results, but I deliberately haven't quoted their numbers, because none of those workloads match yours.

## 10. References (verified links)

**IVM and incremental computation:**
- [Gupta & Mumick 1995](https://www.researchgate.net/publication/2579074_Maintenance_of_Materialized_Views_Problems_Techniques_and_Applications)
- [DBSP, PVLDB 2023](https://www.vldb.org/pvldb/vol16/p1601-budiu.pdf), [DOI](https://dl.acm.org/doi/10.14778/3587136.3587137)
- [Differential Dataflow, CIDR 2013](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper111.pdf)
- [DBToaster, PVLDB 2012](http://vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf)
- [Noria, OSDI 2018](https://www.usenix.org/conference/osdi18/presentation/gjengset)
- [Olteanu 2024](https://arxiv.org/abs/2404.17679)
- [Enzyme 2026](https://arxiv.org/html/2603.27775v1)
- [Snowflake DVS, SIGMOD 2025](https://dl.acm.org/doi/10.1145/3722212.3724455)
- [OpenIVM](https://arxiv.org/html/2404.16486v1)

**Real-time analytics systems:**
- [Mesa](https://research.google/pubs/mesa-geo-replicated-near-real-time-scalable-data-warehousing/)
- [Napa](http://www.vldb.org/pvldb/vol14/p2986-sankaranarayanan.pdf)
- [Druid](https://dl.acm.org/doi/10.1145/2588555.2595631)
- [Pinot](https://dl.acm.org/doi/10.1145/3183713.3190661)
- [ClickHouse, PVLDB 2024](https://www.vldb.org/pvldb/vol17/p3731-schulze.pdf)
- [Trill](https://dl.acm.org/doi/10.14778/2735496.2735503)

**CDC and consistency:**
- [DBLog](https://arxiv.org/abs/2010.12597)
- [Theoretical Study of DBLog](https://arxiv.org/abs/2605.31475)
- [Generalized DBLog](https://arxiv.org/abs/2609.08160)
- [Photon](https://research.google.com/pubs/archive/41318.pdf)
- [Epoxy](https://dl.acm.org/doi/pdf/10.14778/3611479.3611484)

**Multi-tenancy:**
- [Aulbach et al. 2008](https://dl.acm.org/doi/10.1145/1376616.1376736)
- [SQLVM](https://www.cidrdb.org/cidr2013/Papers/CIDR13_Paper25.pdf)

**Top-k and sketches:**
- [Yi et al. 2003](https://ieeexplore.ieee.org/document/1260792/)
- [Mouratidis et al. 2006](https://dl.acm.org/doi/10.1145/1142473.1142544)
- [Space-Saving](https://link.springer.com/chapter/10.1007/978-3-540-30570-5_27)
- [SpaceSaving±](https://arxiv.org/abs/2309.12623)
- [Mergeable Summaries](https://dl.acm.org/doi/abs/10.1145/2213556.2213562)
- [HyperLogLog](https://algo.inria.fr/flajolet/Publications/FlFuGaMe07.pdf)
- [AQP: No Silver Bullet](https://dl.acm.org/doi/10.1145/3035918.3056097)
- [Data Cube](https://link.springer.com/article/10.1023/A:1009726021843)

**Systems documentation:**
- [pg_ivm](https://github.com/sraoss/pg_ivm)
- [PG REFRESH MV](https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html)
- [PG publications/replica identity](https://www.postgresql.org/docs/current/logical-replication-publication.html)
- [PG row filters](https://www.postgresql.org/docs/current/logical-replication-row-filter.html)
- [PG replication config](https://www.postgresql.org/docs/current/runtime-config-replication.html)
- [PG RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [Debezium PG](https://debezium.io/documentation/reference/stable/connectors/postgresql.html)
- [Debezium Mongo](https://debezium.io/documentation/reference/stable/connectors/mongodb.html)
- [Mongo change streams](https://www.mongodb.com/docs/manual/changestreams/)
- [Mongo on-demand MVs](https://www.mongodb.com/docs/manual/core/materialized-views/)
- [ClickHouse incremental MVs](https://clickhouse.com/docs/concepts/features/materialized-views/incremental-materialized-view)
- [ClickHouse VersionedCollapsingMergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/versionedcollapsingmergetree)
- [ClickHouse workload scheduling](https://clickhouse.com/docs/concepts/features/configuration/server-config/workload-scheduling)
- [Timescale real-time aggregates](https://www.tigerdata.com/docs/learn/continuous-aggregates/real-time-aggregates)
- [Timescale hierarchical aggregates](https://www.tigerdata.com/docs/learn/continuous-aggregates/hierarchical-continuous-aggregates)
- [Materialize isolation](https://materialize.com/docs/serve-results/isolation-level/)
- [Materialize self-correction](https://materialize.com/blog/self-correcting-materialized-views/)
- [Materialize community license](https://materialize.com/self-managed/community-license/)
- [RisingWave PG CDC](https://docs.risingwave.com/ingestion/sources/postgresql/pg-cdc)
- [RisingWave repo](https://github.com/risingwavelabs/risingwave)
- [Feldera](https://github.com/feldera/feldera)
- [Pinot upsert](https://docs.pinot.apache.org/build-with-pinot/ingestion/upsert-dedup/upsert)
- [Pinot tenants](https://docs.pinot.apache.org/architecture-and-concepts/components/cluster/tenant)
- [Druid mixed workloads](https://druid.apache.org/docs/latest/operations/mixed-workloads/)
- [DuckDB Postgres extension](https://duckdb.org/docs/2.0/core_extensions/postgres/overview.html)
- [Redis leaderboards](https://redis.io/docs/latest/develop/use-cases/leaderboard/)