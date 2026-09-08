"""Bar policy (spec §2). 24x7 UTC buckets; SessionCalendar is a v2 addition."""
import re

BAR_POLICY_VERSION = "v1:first-print-open/carry-forward-zero-volume"
_UNITS = {"D": 86_400_000, "W": 7 * 86_400_000}
# Epoch (1970-01-01) is a Thursday; the next Monday is 4 days later
# (1970-01-05). Weekly buckets are Monday-anchored (engine + TradingView both
# partition 24x7 weeks Monday-first), not epoch-Thursday-anchored.
_MONDAY_EPOCH_OFFSET_MS = 4 * 86_400_000

# F1: tf_ms is reached (via engine/handle.py's run_full guard) right before a
# C++ stoi cast that aborts the whole process on anything it can't parse --
# tf_ms must accept a STRICT subset of what stoi accepts, never a superset.
# Two escapes were reproduced against the real engine: a decimal-looking but
# non-ASCII digit string ("١٥", Arabic-Indic 15 -- str.isdigit()/int() both
# accept it, stoi does not) and an in-range-for-Python-int multiplier that
# overflows stoi's int range ("99999999999"). Fixed by (a) a closed ASCII
# grammar -- one or more ASCII digits with an optional D/W suffix, or a bare
# D/W with an implied multiplier of 1 -- and (b) bounding the multiplier to
# a generous but finite ceiling. No stripping: a form like "15 " or " 15"
# that used to work only because of an incidental .strip() is now rejected
# too, closing the grammar rather than special-casing whitespace.
_TF_RE = re.compile(r"^[0-9]+[DW]?$")
_BARE_UNIT_RE = re.compile(r"^[DW]$")
_MAX_MULT = 1_000_000

def tf_ms(tf: str) -> int:
    if not isinstance(tf, str) or not tf.isascii():
        raise ValueError(f"bad timeframe {tf!r}")
    if _BARE_UNIT_RE.match(tf):
        mult, unit = 1, tf
    elif _TF_RE.match(tf):
        if tf[-1] in _UNITS:
            mult, unit = int(tf[:-1]), tf[-1]
        else:
            mult, unit = int(tf), None
    else:
        raise ValueError(f"bad timeframe {tf!r}")
    if not (0 < mult <= _MAX_MULT):
        raise ValueError(f"bad timeframe {tf!r}")
    return mult * (_UNITS[unit] if unit else 60_000)

def bucket_start(ts_ms: int, tf: str) -> int:
    m = tf_ms(tf)
    if tf[-1] == "W":
        return ts_ms - ((ts_ms - _MONDAY_EPOCH_OFFSET_MS) % m)
    return ts_ms - (ts_ms % m)
