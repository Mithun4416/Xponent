#!/usr/bin/env python3
"""Generate a replayable mutation stream for freshness / retraction / insert tests (task T2).

Deterministic given --seed. Emits a TSV where each line is one operation to
apply to the primary DB *after* seeding, so CDC must propagate it:

    op <TAB> order_id <TAB> field1 <TAB> field2 ...

Ops:
    cancel   order_id                      placed/shipped -> cancelled
    ship     order_id                      placed         -> shipped
    amount   order_id  new_amount_cents     amount change (bucket-stable)
    insert   order_id  tenant_id  customer_id  status  amount_cents  created_at_iso

Expected deltas are implicit (the applier records the real commit timestamp per
row at apply time, giving freshness ground truth). The correctness oracle is a
direct SQL query over the primary at a quiesced snapshot.

Usage:
    python3 scripts/gen_mutations.py --rows 1000000 --tenants 10 --seed 42 \
        --count 500 --out results/mutations.tsv
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta

from synthetic import (
    GenParams,
    customer_block_start,
    gen_order,
    pareto_weights,
    pick_tenant,
    _cumulative,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate an Xponent mutation stream")
    ap.add_argument("--rows", type=int, required=True, help="order count used at seed time")
    ap.add_argument("--tenants", type=int, required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--span-days", type=int, default=120)
    ap.add_argument("--skew-alpha", type=float, default=1.16)
    ap.add_argument("--count", type=int, default=500, help="number of mutations to emit")
    ap.add_argument("--insert-frac", type=float, default=0.3)
    ap.add_argument("--cancel-frac", type=float, default=0.4)
    ap.add_argument("--ship-frac", type=float, default=0.2)
    ap.add_argument("--out", default="results/mutations.tsv")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    a = _parse_args(argv)
    p = GenParams(rows=a.rows, tenants=a.tenants, seed=a.seed,
                  span_days=a.span_days, skew_alpha=a.skew_alpha)
    cum = _cumulative(pareto_weights(p.tenants, p.skew_alpha))
    # Mutation RNG is independent of the seed RNG so the two streams don't alias.
    r = random.Random((a.seed << 1) ^ 0xA5A5A5A5)

    lines: list[str] = []
    next_insert_id = a.rows + 1
    for _ in range(a.count):
        roll = r.random()
        if roll < a.insert_frac:
            oid = next_insert_id
            next_insert_id += 1
            tenant_id = pick_tenant(r, cum)
            customer_id = customer_block_start(tenant_id, p) + r.randrange(p.customers_per_tenant)
            created = p.start + timedelta(seconds=r.randrange(p.span_days * 86400),
                                          microseconds=r.randrange(1_000_000))
            amount = r.randint(500, 50000)
            lines.append(f"insert\t{oid}\t{tenant_id}\t{customer_id}\tplaced\t{amount}\t{created.isoformat()}")
        elif roll < a.insert_frac + a.cancel_frac:
            oid = r.randint(1, a.rows)
            lines.append(f"cancel\t{oid}")
        elif roll < a.insert_frac + a.cancel_frac + a.ship_frac:
            oid = r.randint(1, a.rows)
            lines.append(f"ship\t{oid}")
        else:
            oid = r.randint(1, a.rows)
            # Re-derive the order to produce a plausible new amount (bucket-stable).
            base = gen_order(oid, p, cum).total_amount_cents
            new_amt = max(0, base + r.randint(-5000, 5000))
            lines.append(f"amount\t{oid}\t{new_amt}")

    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[mutations] wrote {len(lines)} ops -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
