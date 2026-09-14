# Verified replay archive publication plan

## Objective

Publish a small number of independently verifiable, immutable strategy replay
archives. An archive must let a researcher inspect the frozen configuration,
observed inputs, replay-derived decisions, outcomes, and integrity proofs. It
must not expose credentials, wallets, live databases, or imply that a simulated
result is an executable trading result.

The existing operator workflow is documented in
[replay-packages.md](replay-packages.md). This plan governs when it is safe to
use it for a public release.

## Phase 1 — Select a suitable candidate

1. Choose one Pump.fun strategy instance from a single implementation and
   contract era. Start with a bounded interval, not its entire lifetime.
2. Record the instance ID, exact time range, implementation hash, source
   archive identity, expected package-size budget, and why the sample is useful.
3. Refuse a candidate with missing input history, mixed source eras, mutable
   source files, non-empty SQLite WAL/journal sidecars, or an unverified
   checkpoint boundary. Label a checkpointed release `checkpoint-forward-only`.

**Exit criterion:** an operator approves the candidate and its intended public
scope in a short release record.

## Phase 2 — Freeze and build offline

1. Create an integrity-checked frozen backup outside the running service.
2. Run `scripts/research_package.py create` against read-only copies of the
   ledger and shared input archive, with `--frozen-source` and explicit input,
   time, and output-size limits.
3. Keep the output private. The creation process must never run in the serving
   process or mutate a live ledger.
4. Record the command, tool version, source hashes, frozen-through time, output
   byte count, and initial SHA-256 in the release record.

**Exit criterion:** creation produces a complete manifest; a budget failure or
replay failure produces no candidate for publication.

## Phase 3 — Verify and review

1. Run `scripts/research_package.py verify` in a clean environment with the
   matching trusted source release.
2. Have a second operator or isolated job repeat verification from the frozen
   source. Both runs must match final state and ordered table proofs.
3. Review `contract.json`, `manifest.json`, inputs, decisions, outcomes, and
   package metadata for credentials, account identifiers, private endpoints,
   personally identifying data, misleading labels, and future data mixed into
   entry-time features.
4. Confirm the archive describes modeled execution and its limits prominently.

**Exit criterion:** two successful verifications and a completed public-inputs
review are attached to the release record.

## Phase 4 — Publish immutably

1. Use `scripts/publish_package.py` with its explicit
   `--reviewed-public-inputs` acknowledgement.
2. Create a bounded immutable ZIP, calculate its SHA-256, and atomically add it
   to the public package registry. The registry entry should include instance,
   frozen-through time, byte size, hash, schema/version, and limitations.
3. Verify the public endpoint returns the same hash and supports a normal and a
   byte-range download. Keep archive generation disabled in the web service.
4. Announce only the archive’s evidence scope—not performance claims or a trade
   recommendation.

**Exit criterion:** `/api/public/packages` lists the package and
`/api/public/packages/<sha256>.zip` passes a clean download-and-hash check.

## Phase 5 — Product presentation and operations

1. The Research page lists an archive only after it is in the registry; each
   item links directly to its immutable ZIP and displays frozen-through time,
   size, and SHA-256.
2. The general **Browse all lab data** link remains available for JSONL exports
   and snapshots. Empty replay areas remain silent until a package exists.
3. Run a scheduled registry audit: download/hash check, manifest schema check,
   range-request check, and link check.
4. If a release is found to be unsafe or incorrectly labeled, remove it from the
   registry immediately, publish a correction notice, preserve the release
   record, and never replace bytes under the same SHA-256.

## First-release recommendation

Use a short, single-era Pump.fun instance with an intentionally conservative
size cap. Do not publish the existing full-day rehearsal as the first browser
archive: its documented compressed size is about 474 MiB. Treat it as a private
verification rehearsal until a deliberately bounded public sample clears the
review gates above.
