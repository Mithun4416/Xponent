#!/usr/bin/env python3
"""CDC worker main loop (task T4): wal2json logical replication -> coalescer -> atomic flush.

Transport decision (D6): the primary streams logical changes via the wal2json
plugin (structured JSON, full before-images under REPLICA IDENTITY FULL), which
is far more reliable to parse in Python than hand-decoding the pgoutput binary
protocol. The transport is isolated here; the coalescer/flusher are plugin-
agnostic, so swapping to pgoutput later touches only this file.

Pipeline:
  * Stream wal2json format-version 2 messages (psycopg2 LogicalReplicationConnection).
  * Normalize each to a ChangeEvent; DROP events with lsn <= last_applied_lsn
    PER EVENT (fix H1) before coalescing.
  * Coalesce into a micro-batch (~100 ms or N events); flush atomically to the
    summary store (worker/flusher.flush); only then send replication feedback so
    WAL is not released before the summary commit is durable.
  * When idle and caught up, advance just the freshness watermark (heartbeat, fix H5).

Run (inside the worker container / venv):
    python3 -m worker.cdc_processor
Env: PRIMARY_DB_URL, SUMMARY_DB_URL, CDC_SLOT_NAME.
"""
from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
from datetime import datetime, timezone

import psycopg                      # v3, summary writes
import psycopg2                     # v2, logical replication streaming
from psycopg2.extras import LogicalReplicationConnection

from common.registry import SourceEntry, load_sources
from worker.coalescer import MicroBatchCoalescer
from worker.events import ChangeEvent, ItemRow, OrderRow
from worker.flusher import flush, heartbeat

SLOT = os.getenv("CDC_SLOT_NAME", "agg_slot")
PRIMARY_DB_URL = os.getenv("PRIMARY_DB_URL", "postgresql://replication_reader:rep_pass@localhost:5432/primary_db")
SUMMARY_DB_URL = os.getenv("SUMMARY_DB_URL", "postgresql://agg_user:agg_password@localhost:5433/agg_db")
BATCH_MS = int(os.getenv("CDC_BATCH_MS", "100"))
BATCH_MAX = int(os.getenv("CDC_BATCH_MAX", "2000"))
HEARTBEAT_S = float(os.getenv("CDC_HEARTBEAT_S", "1.0"))


def _parse_ts(val: str | None) -> datetime:
    if not val:
        return datetime.now(timezone.utc)
    s = val.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cols_to_dict(cols: list[dict]) -> dict:
    return {c["name"]: c.get("value") for c in (cols or [])}


def _tid(d: dict, tenant_override: int | None) -> int:
    # Per-tenant-DB topology: the source database IS the tenant, so tag with the
    # registry tenant_id. Shared-table topology: read it from the row.
    return tenant_override if tenant_override is not None else int(d["tenant_id"])


def _order_row(d: dict, tenant_override: int | None) -> OrderRow:
    return OrderRow(
        tenant_id=_tid(d, tenant_override),
        order_id=int(d["order_id"]),
        status=str(d["status"]),
        total_amount_cents=int(d["total_amount_cents"]),
        created_at=_parse_ts(d["created_at"]),
    )


def _item_row(d: dict, tenant_override: int | None) -> ItemRow:
    return ItemRow(
        tenant_id=_tid(d, tenant_override),
        order_id=int(d["order_id"]),
        product_id=int(d["product_id"]),
        quantity=int(d["quantity"]),
        line_amount_cents=int(d["line_amount_cents"]),
        order_created_at=_parse_ts(d["order_created_at"]),
    )


def _to_event(change: dict, lsn: int, tenant_override: int | None) -> ChangeEvent | None:
    """Map one wal2json v2 change object to a ChangeEvent (or None if irrelevant)."""
    action = change.get("action")            # I / U / D / B / C / M / T
    table = change.get("table")
    if table not in ("orders", "order_items") or action not in ("I", "U", "D"):
        return None
    new_cols = _cols_to_dict(change.get("columns")) if action in ("I", "U") else None
    old_cols = _cols_to_dict(change.get("identity")) if action in ("U", "D") else None
    ts = _parse_ts(change.get("timestamp"))
    if table == "orders":
        new = _order_row(new_cols, tenant_override) if new_cols else None
        old = _order_row(old_cols, tenant_override) if old_cols else None
    else:
        new = _item_row(new_cols, tenant_override) if new_cols else None
        old = _item_row(old_cols, tenant_override) if old_cols else None
    return ChangeEvent(lsn=lsn, commit_ts=ts, table=table, op=action, old=old, new=new)


def _load_checkpoint(summary: psycopg.Connection, slot: str) -> int:
    with summary.cursor() as cur:
        cur.execute("SELECT last_applied_lsn FROM cdc_checkpoint WHERE slot_name = %s", (slot,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _stream(cur, summary, slot: str, tenant_override: int | None, last_applied: int, stop: threading.Event) -> None:
    """Consume one source's replication stream until the connection drops (then
    raise) or stop is set. last_applied is reloaded by the caller on each
    (re)connect, so per-event idempotency (H1) survives reconnects."""
    batch = MicroBatchCoalescer()
    last_flush = time.monotonic()
    last_feedback_lsn = 0
    last_activity = time.monotonic()

    def do_flush() -> None:
        nonlocal last_flush, last_feedback_lsn, last_applied, batch
        if batch.is_empty():
            return
        if flush(summary, slot, batch):
            last_applied = max(last_applied, batch.max_lsn)
            last_feedback_lsn = max(last_feedback_lsn, batch.max_lsn)
            cur.send_feedback(flush_lsn=last_feedback_lsn)  # release WAL only after durable summary commit
        batch = MicroBatchCoalescer()
        last_flush = time.monotonic()

    while not stop.is_set():
        msg = cur.read_message()
        if msg is None:
            if not batch.is_empty() and (time.monotonic() - last_flush) * 1000 >= BATCH_MS:
                do_flush()
            if time.monotonic() - last_activity >= HEARTBEAT_S:
                heartbeat(summary, slot, datetime.now(timezone.utc))
                last_activity = time.monotonic()
            select.select([cur], [], [], max(0.0, HEARTBEAT_S))
            continue

        last_activity = time.monotonic()
        try:
            payload = json.loads(msg.payload)
        except (ValueError, TypeError):
            continue
        ev = _to_event(payload, int(msg.data_start), tenant_override)
        if ev is not None and ev.lsn > last_applied:   # per-event idempotency (H1)
            batch.apply(ev)

        if batch.event_count >= BATCH_MAX or (time.monotonic() - last_flush) * 1000 >= BATCH_MS:
            do_flush()


def _connect_summary(label: str = "cdc") -> psycopg.Connection:
    """Connect to the summary DB, waiting with backoff until it is reachable.

    On Docker restarts the worker can start before pg-summary's DNS name or
    port is ready; crashing there loses the whole process, so wait instead.
    """
    backoff = 1.0
    while True:
        try:
            return psycopg.connect(SUMMARY_DB_URL, autocommit=True)
        except psycopg.OperationalError as e:
            print(f"[{label}] summary DB not reachable yet ({e}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)


def _ensure_activity_table() -> None:
    """Create cdc_activity if missing (the summary volume predates this table)."""
    with _connect_summary() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cdc_activity ("
            " id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, op VARCHAR(8) NOT NULL,"
            " tbl VARCHAR(32) NOT NULL, order_id BIGINT, lsn BIGINT NOT NULL,"
            " commit_ts TIMESTAMPTZ, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cdc_activity_at ON cdc_activity (applied_at DESC)")


def run_source(source: SourceEntry, stop: threading.Event) -> None:
    """Stream one tenant source into the central summary, reconnecting with backoff.
    One of these runs per registry entry (one per tenant database)."""
    summary = _connect_summary(f"cdc:{source.label}")
    backoff = 1.0
    try:
        while not stop.is_set():
            repl = None
            try:
                if summary.closed or summary.broken:
                    # The summary DB restarted under us: reconnect before resuming.
                    summary = _connect_summary(f"cdc:{source.label}")
                repl = psycopg2.connect(source.dsn, connection_factory=LogicalReplicationConnection)
                cur = repl.cursor()
                last_applied = _load_checkpoint(summary, source.slot)
                print(f"[cdc:{source.label}] slot={source.slot} resuming from last_applied_lsn={last_applied}")
                cur.start_replication(
                    slot_name=source.slot,
                    decode=True,
                    options={
                        "format-version": "2",
                        "include-timestamp": "true",
                        "actions": "insert,update,delete",
                        "add-tables": "public.orders,public.order_items",
                    },
                )
                backoff = 1.0
                print(f"[cdc:{source.label}] streaming wal2json changes...")
                _stream(cur, summary, source.slot, source.tenant_id, last_applied, stop)
            except Exception as e:
                if stop.is_set():
                    break
                print(f"[cdc:{source.label}] stream error: {e!r}; reconnecting in {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                if repl is not None:
                    try:
                        repl.close()
                    except Exception:
                        pass
    finally:
        summary.close()


def run() -> int:
    """Start one streaming thread per tenant source from the registry.

    TENANT_SOURCES unset => a single shared source (original topology).
    TENANT_SOURCES set => one source (database + slot) per tenant.
    """
    sources = load_sources(PRIMARY_DB_URL, SLOT)
    print(f"[cdc] {len(sources)} source(s): " + ", ".join(f"{s.label}:{s.slot}" for s in sources))
    _ensure_activity_table()
    stop = threading.Event()

    if len(sources) == 1:
        try:
            run_source(sources[0], stop)
        except KeyboardInterrupt:
            print("[cdc] stopped.")
        return 0

    threads = [threading.Thread(target=run_source, args=(s, stop), name=f"cdc-{s.label}", daemon=True)
               for s in sources]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[cdc] stopping all sources...")
        stop.set()
        for t in threads:
            t.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(run())
