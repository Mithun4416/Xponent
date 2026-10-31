"""Config-defined derived metrics (Bonus B-4): add a metric by editing
`metrics.json`, not by changing code.

Each metric has a `name`, `label`, an arithmetic `expr` over a fixed set of base
variables (orders, placed, shipped, cancelled, gross, net), and a display
`format`. Formulas are evaluated with a restricted AST walker — only names from
the whitelist, numeric literals and + - * / and parentheses are allowed, so an
edited config file can never execute arbitrary code. The file is re-read when its
mtime changes, so edits take effect without a restart.
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass

from app.core import config

_ALLOWED_VARS = {"orders", "placed", "shipped", "cancelled", "gross", "net"}


@dataclass
class MetricDef:
    name: str
    label: str
    expr: str
    fmt: str


_cache: tuple[float, list[MetricDef]] | None = None


def _safe_eval(node: ast.AST, variables: dict[str, float]) -> float:
    """Evaluate a tiny arithmetic AST. Raises ValueError on anything unexpected."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, variables)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left, right = _safe_eval(node.left, variables), _safe_eval(node.right, variables)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right if right else 0.0  # div-by-zero -> 0, never crash
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _safe_eval(node.operand, variables)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in _ALLOWED_VARS:
            raise ValueError(f"unknown variable: {node.id}")
        return float(variables.get(node.id, 0))
    raise ValueError("unsupported expression")


def load_metrics() -> list[MetricDef]:
    """Read (and cache by mtime) the metric definitions from the config file."""
    global _cache
    path = config.METRICS_CONFIG_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _cache and _cache[0] == mtime:
        return _cache[1]
    defs: list[MetricDef] = []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for m in raw.get("metrics", []):
            # Validate the formula once at load time so a bad config is caught early.
            ast.parse(m["expr"], mode="eval")
            defs.append(MetricDef(m["name"], m.get("label", m["name"]), m["expr"],
                                  m.get("format", "number")))
    except Exception as e:  # malformed config -> no derived metrics, never a 500
        print(f"[metrics] config error in {path}: {e}", flush=True)
        defs = []
    _cache = (mtime, defs)
    return defs


def compute(variables: dict[str, float]) -> list[dict]:
    """Evaluate every configured metric against the given base variables."""
    out = []
    for m in load_metrics():
        try:
            value = round(_safe_eval(ast.parse(m.expr, mode="eval"), variables), 2)
        except Exception:
            value = None
        out.append({"name": m.name, "label": m.label, "value": value, "format": m.fmt})
    return out
