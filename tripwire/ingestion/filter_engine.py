"""Filter engine for trigger-specific predicates.

Evaluates a list of filter rules against decoded event fields.
All filters must pass (AND logic) for the event to match.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def evaluate_filters(
    decoded: dict[str, Any],
    filters: list[Any],
) -> tuple[bool, str | None]:
    """Evaluate all filter rules against decoded event data.

    Returns (passed, rejection_reason).
    """
    if not filters:
        return True, None

    for f in filters:
        field = f.field if hasattr(f, "field") else f.get("field")
        op = f.op if hasattr(f, "op") else f.get("op", "eq")
        value = f.value if hasattr(f, "value") else f.get("value")

        field_val = decoded.get(field)
        if field_val is None:
            return False, f"field '{field}' not present in event"

        ok = _evaluate_op(field_val, op, value)
        if not ok:
            return False, f"{field} {op} {value} failed (got {field_val})"

    return True, None


def _evaluate_op(field_val: Any, op: str, target: Any) -> bool:
    if op == "eq":
        return _normalize(field_val) == _normalize(target)
    elif op == "neq":
        return _normalize(field_val) != _normalize(target)
    elif op in ("gt", "gte", "lt", "lte"):
        return _compare_numeric(field_val, op, target)
    elif op == "in":
        if not isinstance(target, list):
            return False
        norm = _normalize(field_val)
        return norm in [_normalize(t) for t in target]
    elif op == "not_in":
        if not isinstance(target, list):
            return True
        norm = _normalize(field_val)
        return norm not in [_normalize(t) for t in target]
    elif op == "between":
        if not isinstance(target, list) or len(target) != 2:
            return False
        a = _to_decimal(field_val)
        lo = _to_decimal(target[0])
        hi = _to_decimal(target[1])
        if a is None or lo is None or hi is None:
            return False
        return lo <= a <= hi
    elif op == "contains":
        return str(target).lower() in str(field_val).lower()
    elif op == "regex":
        try:
            return bool(re.search(str(target), str(field_val)))
        except re.error:
            return False
    else:
        logger.warning("unknown_filter_op", op=op)
        return False


def _normalize(val: Any) -> Any:
    if isinstance(val, str):
        s = val.lower().strip()
        if _ADDRESS_RE.match(s):
            return s
        # Try numeric normalization for string-encoded integers
        d = _to_decimal(s)
        if d is not None:
            return d
        return s
    return val


def _to_decimal(val: Any) -> Decimal | None:
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return None
        if v.startswith("0x") and not _ADDRESS_RE.match(v):
            try:
                return Decimal(int(v, 16))
            except (ValueError, InvalidOperation):
                return None
        try:
            return Decimal(v)
        except InvalidOperation:
            return None
    return None


def _compare_numeric(field_val: Any, op: str, target: Any) -> bool:
    a = _to_decimal(field_val)
    b = _to_decimal(target)
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    elif op == "gte":
        return a >= b
    elif op == "lt":
        return a < b
    elif op == "lte":
        return a <= b
    return False
