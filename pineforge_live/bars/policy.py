"""Bar policy (spec §2). 24x7 UTC buckets; SessionCalendar is a v2 addition."""
BAR_POLICY_VERSION = "v1:first-print-open/carry-forward-zero-volume"
_UNITS = {"D": 86_400_000, "W": 7 * 86_400_000}
# Epoch (1970-01-01) is a Thursday; the next Monday is 4 days later
# (1970-01-05). Weekly buckets are Monday-anchored (engine + TradingView both
# partition 24x7 weeks Monday-first), not epoch-Thursday-anchored.
_MONDAY_EPOCH_OFFSET_MS = 4 * 86_400_000

def tf_ms(tf: str) -> int:
    tf = tf.strip()
    if not tf:
        raise ValueError(f"bad timeframe {tf!r}")
    if tf[-1] in _UNITS:
        mult = tf[:-1] or "1"
        if not mult.isdigit():
            raise ValueError(f"bad timeframe {tf!r}")
        ms = int(mult) * _UNITS[tf[-1]]
    else:
        if not tf.isdigit():
            raise ValueError(f"bad timeframe {tf!r}")
        ms = int(tf) * 60_000
    if ms <= 0:
        raise ValueError(f"bad timeframe {tf!r}")
    return ms

def bucket_start(ts_ms: int, tf: str) -> int:
    m = tf_ms(tf)
    if tf.strip()[-1] == "W":
        return ts_ms - ((ts_ms - _MONDAY_EPOCH_OFFSET_MS) % m)
    return ts_ms - (ts_ms % m)
