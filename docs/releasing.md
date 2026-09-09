# Preparing a public release

PineForge Live is prepared as a standalone Apache-2.0 project. Preparing a
candidate does not publish its repository, create a tag or upload a package.
Version `0.1.0` is pre-alpha; describe the verified behavior and limitations
from the [README](../README.md), not production-readiness guarantees.

## Check a candidate

1. Confirm the intended commit and a clean working tree. Inspect tracked files
   and Git history before making the repository public; a source archive and
   a public Git history expose different content.
2. Run the CI-equivalent synthetic suite and Node export tests described in
   [CONTRIBUTING](../CONTRIBUTING.md). Record platform, Python version and
   skipped engine fixtures. If behavior affecting strategy execution changes,
   obtain the corresponding real-engine/Cloud Run evidence separately.
3. Build both distributions with `python -m build`, then run
   `python -m twine check --strict dist/*`. Check that `LICENSE` and `NOTICE`
   retain their exact bytes, and that examples and linked guides are present
   in the source archive. Install the wheel in a fresh environment and run
   `pineforge-live version` from outside the source checkout.
4. Scan reachable Git history and distribution contents for credentials and
   private data. For example, use `gitleaks git . --log-opts='--all' --redact`.
   Keep its reports outside tracked files. Inspect findings; a scanner pass
   is not proof that every form of private information has been identified.
5. Obtain the maintainer's independent review of the exact candidate. The
   current review process includes Grok; contributor PRs do not need to pay
   for or operate that review service. Bind release notes to the reviewed and
   tested commit, and preserve unresolved limitations.

Never include `build/` contents, SQLite journals, `.env` files, broker keys,
webhook secrets or private campaign exports. Public verification receipts
contain identifiers, hashes and counts; they do not grant redistribution
rights to the original strategies or market data.

## Configure the public repository

Before changing repository visibility or pushing a public release:

- Confirm the repository owner/name and publication authorization. Link the
  README, license and contribution guide from the repository landing page.
- Enable Issues and private vulnerability reporting, and verify a maintainer
  receives private reports. Add the confirmed reporting channel to a security
  policy before announcing the project; do not invent a contact address or
  response-time commitment.
- Require review and the relevant CI checks for the default branch. Workflow
  tokens need only read access for tests; untrusted PR jobs must receive no
  cloud, broker or publishing secrets.
- Decide the package index and release process. The prepared CI builds and
  validates packages but does not upload to PyPI or publish GitHub releases.
  If publishing is later enabled, use a separately reviewed release workflow
  and a protected environment or trusted publishing identity.

The public runtime and engine are Apache-2.0. The separate codegen project is
source-available with additional commercial terms. Do not label the entire
compiler toolchain as unrestricted open source. Any separately distributed
native binaries, generated strategies or data retain their own notices.

## Record the release

After authorized publication, verify the actual repository visibility, tag,
downloaded distribution hashes and installed CLI. Record what was published
and the exact source identity. A local test pass, CI pass, repository push,
package upload and broker execution are distinct outcomes.
