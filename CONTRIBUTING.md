# Contributing to PineForge Live

PineForge Live is a broker-neutral strategy runtime. Contributions to feeds,
webhook delivery, documentation and reproducible replay cases are welcome.
The C++ engine remains the authority for strategy evaluation and simulated
fills. Fixes to those rules belong in
[pineforge-engine](https://github.com/pineforge-4pass/pineforge-engine).

## Set up a development checkout

Use Python 3.12 or newer on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,websocket]'
env -u PINEFORGE_ENGINE_ROOT python -m pytest
```

The default suite uses synthetic inputs, fake strategy decisions and local
HTTP/WebSocket receivers. Tests needing compiled strategies explicitly skip
when `PINEFORGE_ENGINE_ROOT` is absent. The optional WebSocket dependency is
included above so transport tests run. No broker account or cloud credentials
are needed. CI is configured to check Python 3.13/3.14 and macOS compatibility.

If changing the campaign export helper, use Node.js 22 or newer to run its
offline tests:

```sh
node --test tests/test_export_campaign.mjs
```

For changes requiring real engine behavior, see the pinned build instructions
in the [README](README.md#install). Keep synthetic/unit evidence separate from
real-probe measurements. Maintainer campaign probes and parity grading run
through the isolated [Cloud Run workflow](cloudrun/README.md); a contributor
does not need access to that infrastructure to submit a fix.

## Report a bug or propose a change

Include the runtime and engine versions, operating system, feed mode and a
small reproduction. Describe what happened and what you expected. Share
only configuration, strategies and data you have permission to redistribute;
remove keys, webhook secrets, account details and private URLs from logs.
For security-sensitive issues, use GitHub's
[private vulnerability reporting](https://github.com/pineforge-4pass/pineforge-live/security/advisories/new)
instead of a public issue.

Discuss substantial API changes before implementing them. Keep pull requests
focused, explain the resulting behavior and include relevant test results.
State which checks skipped and why. Treat reviewers and contributors
respectfully, and discuss technical disagreements using concrete examples.

## Preserve the runtime contracts

- Market data is caller-supplied. Feed adapters normalize input; they do not
  introduce another strategy or simulated-fill implementation.
- Webhook acceptance is delivery evidence. A broker's order, fill and account
  state remain the bridge's responsibility.
- Stable event IDs, ordered delivery and atomic restart state must survive
  retries and process recovery. Receivers still need deduplication.
- Missing input must follow the declared gap policy. Do not invent trades,
  change source rows or shorten closing boundaries to make a replay pass.
- A test of tick mode needs actual ticks. An empty action window does not
  establish order-delivery coverage. Keep input equivalence, native-chart
  equality and broker execution as separate claims.

Add a regression test when fixing behavior that can lose, duplicate or change
an action. For documentation-only changes, check links and copyable commands;
a new test that merely repeats the documentation is unnecessary.

## Build distributions

```sh
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

The wheel contains the runtime, metadata, `LICENSE` and `NOTICE`. The source
distribution also includes examples, guides, tests and maintainer scripts.
Keep local build artifacts, journals, credentials and private campaign inputs
out of both distributions. See [release preparation](docs/releasing.md) for
maintainer checks.

## License of contributions

Unless explicitly stated otherwise and agreed before submission,
contributions intentionally submitted for inclusion are provided under this
repository's [Apache-2.0 license](LICENSE), as described in its Section 5.
Only submit work you have the right to contribute, and preserve applicable
copyright and attribution notices. This does not change the separate
licenses of the engine, compiler, strategies or market data.
