# Replay feeds and session calendars

`mock-feed` reads the original six-column CSV without a broker connection or
strategy execution:

```csv
timestamp,open,high,low,close,volume
1788912000000,2500,2505,2498,2502,12
1788912060000,2502,2508,2501,2506,9
```

Generate either input from the same minute file:

```sh
pineforge-live mock-feed minutes.csv --input-mode bars --output minutes.jsonl
pineforge-live mock-feed minutes.csv --input-mode ticks --policy high-first --output ticks-high.jsonl
pineforge-live mock-feed minutes.csv --input-mode ticks --policy low-first --output ticks-low.jsonl
```

Use `source: {"kind":"jsonl","path":"minutes.jsonl"}` and `input_mode: "bars"`
in a complete configuration, or point it to a tick file and select `"ticks"`.
Then run it normally:

```sh
pineforge-live run --config signals.json
```

For stdin, configure `source: {"kind":"stdin"}` and pipe the mock stream:

```sh
pineforge-live mock-feed minutes.csv --input-mode ticks --policy high-first |
  pineforge-live run --config signals-stdin.json
```

Each positive-volume minute produces O→H→L→C or O→L→H→C ticks with the exact
minute OHLC and total volume, followed by its original minute boundary.
`--policy seeded --seed 7` chooses a repeatable path per minute. Synthetic
paths are test inputs; 1m OHLCV cannot reveal the historical tick order.
Settled parent results should agree across both paths for the same engine,
settings and complete bars; provisional intrabar actions can differ.

Use `--start-ms` (inclusive) and `--end-ms` (exclusive) to select the new input
after warmup. `--start-seq` sets the first generated tick sequence. Output
files must be new paths and are published only after successful validation.
Use separate journals/configurations for independent replay lanes. Keep the
same file, seed, sequence origin and journal when testing restart recovery.
An incomplete final parent remains forming and emits no settled-bar action.

## Exchange sessions and daily candles

UTC bucket alignment is the default. For sessions, holidays, daylight-saving
changes or daily candles with another boundary, provide a price-independent
calendar JSON and add `parent_windows_path: "calendar.json"` to an
`input_tf: "1"` configuration:

```json
[
  {"open_ms":1788912000000,"close_ms":1788935400000},
  {"open_ms":1788998400000,"close_ms":1789021800000}
]
```

Windows must be ordered, nonoverlapping and minute-aligned. Include the entire
warmup-history prefix and the sessions to run; each historical script bar
must match its window's opening timestamp. Minutes outside those windows are
refused. Gaps between sessions are permitted; missing minutes inside a session
are refused by default. Each parent closes at its declared `close_ms`, even
when the session is shorter than the nominal strategy timeframe.

A session calendar can optionally include `first_minute_ms` when a native
bar's timestamp precedes market open, for example a 17:00 daily label whose
first trade minute is 18:00. This time must fall within the parent window.
The emitted parent keeps `open_ms` as its label; every minute from
`first_minute_ms` up to (but excluding) `close_ms` remains required by default.
Use the same calendar for real feeds and `mock-feed`; do not use this field
to hide missing data.

Pass the same calendar to `mock-feed --parent-windows calendar.json`. Its
tick sequence continues across session gaps. The schedule is bound into the
deployment identity; compact checkpoints reference its digest. Exhausting a
finite schedule stops new input. Supply an adequate future calendar before
starting a deployment. A replay calendar derived from reference timestamps
must be described as such when reporting verification.

Your webhook receiver owns broker routing, order submission and execution
acknowledgments. Start with the included receiver or your paper-trading
bridge, confirm the emitted symbol/side/quantity sequence, then point the
configured webhook at your trading bridge. Use receiver-side `event_id`
deduplication before order submission. Successful webhook delivery and actual
broker fills are separate outcomes.

