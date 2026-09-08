"""Physical order identity, distinct from the Pine order's display id."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
from enum import Enum


def canonical(value) -> str:
    def encode(obj):
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        raise TypeError(f"no canonical encoding for {type(obj).__name__}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=encode)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def client_id(epoch_hash: str, intent_key: str, role: str, level_version: int,
              action_seq: int, run_token: int, *, prefix: str = "pf", max_length: int = 36) -> str:
    """Retries use the saved id; only a distinct physical order allocates a seq."""
    if not prefix or not all(c.isascii() and (c.isalnum() or c in "_-") for c in prefix):
        raise ValueError("client-id prefix must contain ASCII letters, digits, '_' or '-'")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0
           for x in (level_version, action_seq, run_token)):
        raise ValueError("identity counters must be nonnegative integers")
    if max_length < len(prefix) + 1 + 32:
        raise ValueError("client-id capacity must preserve at least 128 digest bits")
    return prefix + "_" + digest([epoch_hash, intent_key, role, level_version,
                                  action_seq, run_token])[:max_length - len(prefix) - 1]


def floor_quantity(qty: float, step: float) -> float:
    """Round toward zero on the decimal venue grid; never round an entry up."""
    if isinstance(qty, bool) or isinstance(step, bool) or not math.isfinite(qty) or not math.isfinite(step):
        raise ValueError("quantity and step must be finite numbers")
    if qty < 0 or step <= 0:
        raise ValueError("quantity must be nonnegative and step positive")
    q, s = Decimal(str(qty)), Decimal(str(step))
    return float((q / s).to_integral_value(rounding=ROUND_FLOOR) * s)
