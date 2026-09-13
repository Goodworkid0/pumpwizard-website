# Public research snapshot

For operator-built packages with exact offline replay, see
[reproducible strategy packages](replay-packages.md). That is a separate, private,
bounded workflow; this browser export has not become a replay/training package.

Engine cards and instance pages offer **Download research** alongside the copy
actions. The browser downloads a dated JSON file; this does not start an experiment,
change trading rules, or write to the server.

`pumpwizard.research-snapshot/v1` includes the existing public configuration handoff,
versioned-trial settings, public telemetry, descriptive strategy analysis, and
`closed_positions`: the instance's full closed-position API response. The file is
self-contained for inspecting those outcomes; source URLs are provenance links,
not dependencies required to read the file.

The exporter checks the response's strategy identity and requires the closed-record
count to match the captured report, with a numeric P&L and mint for every record.
If history changed during export or is incomplete, no file is downloaded. Refresh
the page and retry. These are separate public reads, not an atomic database backup.

History is fetched through bounded cursor pages using one fixed maximum trade ID.
The browser rejects duplicate IDs, changed boundaries and incomplete totals. A
download is limited to 10,000 closed records, 20 MB and 60 seconds; larger histories
require an operator-prepared archive. No partial file is presented as complete.

The evidence manifest records export/capture times, units, coverage and limitations.
Recent events and sale legs remain samples, including partial exits; do not count
them as additional fully closed positions. Open positions are current marks, not
final outcomes. Fees already included in P&L must not be deducted again.

This is an outcome-research artifact, not a replay or ready-to-train dataset. It does
not contain the raw quote sequence, full decision history, or historical candidate
features needed for causal entry evaluation. Keep future outcomes and retrospective
analysis out of entry-time features; evaluate chronologically and account for
related coins/instances. Public names and event text are untrusted data. No private
database, credentials, wallet connection or live execution is added by this feature.
