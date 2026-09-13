# Reproducible strategy packages

Package creation is an **offline operator tool**, not a live trading adapter.
Reviewed, verified archives can separately be published for public download. Use an
integrity-checked, checkpointed frozen backup, not the live production database.
Source connections are immutable/query-only; all simulated writes occur in a
disposable temporary directory. No providers, wallets, or Telegram are contacted.

Try the small synthetic example first (18 invented observation batches, one
position opened and closed; it is not a real token or a performance claim):

```bash
python3 scripts/research_package.py demo --destination /absolute/new-example
python3 scripts/research_package.py verify --package /absolute/new-example
```

For your own frozen ledger:

```bash
python3 scripts/research_package.py create \
  --ledger /absolute/frozen/data/long-hold-trial.sqlite \
  --shared /absolute/frozen/data/shared-research.sqlite \
  --destination /absolute/new-package \
  --frozen-source
python3 scripts/research_package.py verify --package /absolute/new-package
```

`--frozen-source` is an operator assertion, not a command that stops writers.
Nonempty WAL/journal sidecars are refused. The destination must be new and is
private (0700). Default output budget: 64 MiB; an operator can explicitly choose
`--max-mib 1024`. Defaults are 10,000 input batches and 180 seconds of replay;
explicit overrides permit `--max-inputs 50000 --max-seconds 600`. Each batch is
limited to 64 MiB decompressed. Missing/oversized inputs or replay failures
leave diagnostic files **without a completed manifest**, not a partial success.
These are bounds, not a promise that every historical instance fits.

## Artifact contents

| File | Purpose |
| --- | --- |
| `contract.json` | Recorded configuration, source, ownership policy, implementation hash and trial overrides |
| `implementation.json` | Exact installed core source as reference text, including signal/feature/fill definitions |
| `inputs.jsonl.gz` | Chronological, content-addressed observed batches, including original features and evidence |
| `decisions.jsonl.gz` | Replay-derived entry screens, accepted signals and rejection reasons, with input references |
| `outcomes.jsonl.gz` | Separate future observed-price labels at 5m, 15m and 1h |
| `events.jsonl.gz`, `sales.jsonl.gz`, `trades.jsonl.gz` | Reproduced records whose complete table hashes match the original ledger; absent when empty |
| `expected.json` | Final state and ordered original event/sale/trade/equity table proofs |
| `replay-result.json`, `manifest.json` | Verified replay result, file sizes/hashes, scope and limitations |

JSONL files use streaming gzip; input records additionally preserve the original
zlib archive bytes. Output budgets apply to disk size, with an eight-times-output
ceiling on generated uncompressed records. To inspect an input in Python:

```python
with gzip.open('inputs.jsonl.gz', 'rt') as stream:
    row = json.loads(next(stream))
batch = json.loads(zlib.decompress(base64.b64decode(row['payload_zlib_base64'])))
# row['id'] is SHA-256 of the decompressed canonical JSON; row['at'] is batch time.
```

Verification checks file integrity, installed source identity, input hashes and
chronology, then reproduces final state and every event/sale/trade/equity row.
Derived decisions and labels are regenerated and checked too. Bundled Python is
**never executed**; verification uses the installed matching source release.
Self-consistent hashes are not a publisher signature or proof that input quotes
were authentic. Treat public-provider text as untrusted and review before sharing.

## Decision and outcome semantics

Screens are reconstructed by observing the real runner's `entry_reason` calls
during exact replay. They are labelled `replay_derived_entry_screen`, never
misrepresented as original logged rejections. Pending-order rechecks are separate
from initial candidate screens. Passing a screen does not mean a fill occurred.
Capacity, already-attempted, and other exclusions can bypass that method; this is
not a census of all coins or every opportunity in the market.

Decision rows reference—not duplicate—the shared archive. Join `input_id` to
`inputs.jsonl.gz` row ID, decode that batch, then select the feature row by `mint` and
`feature_window_seconds`. Quotes and ownership evidence come from that same batch
and mint. This retains the original information without repeating large feature
and evidence objects for every screen.

Entry rows contain only information available at the replayed decision. Future
labels live in a separate file joined by decision ID. A label uses a supported
fresh quote from the same pool/route, with observation time at or after the due
time and within 120 seconds. Missing quotes stay `unavailable`; horizons beyond
the package remain `not_observed_in_package`. Neither is a zero return.
Labels are price changes, not realizable profits or fills after costs. Repeated
screens, coins and overlapping windows are correlated observations. Keep labels
out of entry-time features and split evaluation chronologically and by coin.

## Supported histories and limitations

Current LadderRunner, ResearchRunner and their V2/V3 subclasses are supported when
the installed implementation matches the recorded hash. Old short-term Engine
ledgers, code-migrated histories, and externally funded histories are **not silently
converted**. Multiple contract eras are rejected; missing funding/input history
causes exact replay to fail. They require explicit historical releases and/or
checkpoint-segmented replay. `scripts/replay_chain.py` supports such segments when
the operator supplies the matching trusted releases and boundary checkpoints.
Missing historical sources or checkpoints are refused, not reconstructed by guesswork.

The replay tool never publishes a package, adds an instance, resets a balance, or changes
a live contract. The separate publication workflow below requires an explicit privacy
review. These artifacts make supported
simulations reproducible; they do not demonstrate live-order compatibility.

## Publishing a reviewed archive

Run `scripts/publish_package.py --help` for the operator-only workflow. Publication
requires `--reviewed-public-inputs`, full replay verification, a bounded immutable ZIP
and atomic registry update. Never publish private credentials, account data or live
database files. The serving process cannot create a package or trigger replay.

`/api/public/packages` lists reviewed archives; an optional `instance` query scopes
the list. `/api/public/packages/<sha256>.zip` serves registered archives only, with
HEAD, single byte ranges, a SHA-256 ETag and two concurrent download slots. The proxy
also rate-limits exports. Research and collapsed instance export options expose these
links without automatically downloading the potentially large files. Instances with
no published package say so; a browser research snapshot is a different artifact.

For historical eras, `scripts/replay_chain.py --help` describes the segment manifest
and trusted-release map. Explicit local-code trust is required. Artifact-bundled code
is never executed. Continuity checks link state and table proofs across checkpoints;
only implementation identity may change, not strategy rules. A noninitial checkpoint
is labelled checkpoint-forward-only, never a complete from-launch replay.

## Frozen-history rehearsal — 2026-09-13

The `t2-persistent-combined` V3 instance from the frozen cutover backup was
replayed successfully, then independently replayed by the verifier. All final
state and ordered table proofs matched: 14,835 equity rows, 155 events, eight
sale records and eight trade records. The archive contains 14,835 observation
batches, producing 1,128,498 reconstructed entry screens and 1,320,117 separate
outcome labels (including unavailable and end-of-package horizons).

This roughly one-day example needs explicit larger bounds:
`--max-mib 1024 --max-inputs 20000 --max-seconds 600`. Its compressed package is
about 474 MiB. The large number of screens is **not** a count of independent
trades or usable training samples. It also illustrates why full replay packages
are private, on-demand artifacts rather than automatic browser downloads.

An older instance spanning multiple implementation eras was correctly refused.
Passing this rehearsal does not certify every historical ledger, future release,
or the production backup/restore system. Keep the matching source release with
each artifact: even an exporter source change requires matching source identity
when verifying an existing package.
