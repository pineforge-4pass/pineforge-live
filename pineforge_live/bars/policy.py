"""Bar policy (spec §2). 24x7 UTC buckets; SessionCalendar is a v2 addition."""
BAR_POLICY_VERSION = "v1:first-print-open/carry-forward-zero-volume"
_UNITS = {"D": 86_400_000, "W": 7 * 86_400_000}

def tf_ms(tf: str) -> int:
    tf = tf.strip()
    if tf[-1] in _UNITS:
        return int(tf[:-1] or "1") * _UNITS[tf[-1]]
    return int(tf) * 60_000

def bucket_start(ts_ms: int, tf: str) -> int:
    m = tf_ms(tf)
    return ts_ms - (ts_ms % m)
