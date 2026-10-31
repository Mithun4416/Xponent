"""Back-compat shim: the canonical hash now lives in common/ so both the worker
and the API images share ONE definition (Receipts, B-2). Existing
`from worker.hashing import ...` call sites keep working.
"""
from common.hashing import (  # noqa: F401
    MOD,
    digest_add,
    digest_of_rows,
    digest_remove,
    row_hash64,
)
