# Next-session handover

## Current baseline

- The repository `main` baseline is `fe74ca5` (`Allow compressed public data exports`).
- The production public-worker image is built from `2dc811f` (`Refine public research and download experience`). The Nginx allow-list configuration is from `fe74ca5`; this specifically enables the compressed public export routes.
- The live release is `/opt/pump-paper/releases/20260915-ui-2dc811f` on `idb-host`.
- Check the working tree before making a release. Do not include another session's uncommitted or untracked work merely because it is present locally.

## Production boundary

The public site is a read-only presentation of the existing Pump.fun simulation. This release did **not** alter:

- the Pump.fun collector;
- strategy ledgers or balances;
- live-trading behavior (there is none);
- any Raydium collector or production Raydium simulation.

The deployed public worker is `pump-paper`, with `restart=unless-stopped`. Old public-worker containers were retained as stopped rollback artifacts; inspect their image and configuration before using one for a rollback rather than assuming it includes a newer proxy rule.

After a public-worker restart, `/api/public/health` can briefly be unavailable or report a stale registry until the collector emits a fresh registry. This recovered automatically during deployment; wait for a fresh observation before restarting services again.

## Production verification completed

- `https://pumpwizard.fun/api/public/health` returned healthy.
- Public pages were checked: `/`, `/engine`, `/research`, `/downloads`, `/socials`, `/affiliate`, and `/legal`.
- Public summary/catalog, logo assets, and replay/download endpoints were verified over HTTPS.
- The compressed exports are available at:
  - `/api/public/history.jsonl.gz`
  - `/api/public/research.jsonl.gz`
- Sensitive/non-public routes were checked to return 404, including `/data/service.env`, `/.git/config`, `/acp`, `/api/state`, and `/api/public`.
- External browser checks passed for the Live Data rendering and affiliate page.

## Product state

- PumpWizard Lab now uses one shared public shell: market toggle, Lab branding, Live Data, Live Strategies, Lab Research, Download Lab Data, Community, and `$PUMPWIZARD`.
- Pump.fun is the only enabled live-simulation data lane.
- Raydium CPMM is an intentionally separate **observed/local pilot**. It must never fall back to Pump.fun data or be represented as live before its own evidence and read model are ready.
- The market selection remains visible on shared pages so users learn one UI across markets. Only Live Data and Live Strategies should switch their underlying market data.

## Raydium: start here next session

Follow [Raydium next-session plan](raydium-next-session-plan.md). Its sequence is deliberately local first:

1. Create a reproducible CPMM observation fixture.
2. Run a bounded observed collector.
3. Build an isolated Raydium paper ledger and read model.
4. Publish locally only after evidence gates pass.
5. Verify shared UI contracts, failures, and a soak run.

Do not deploy a Raydium collector, enable public Raydium strategy results, or introduce any real trading while completing those steps. The exit gates in that plan are the release decision; they are not optional formatting work.

## First-session checklist

1. Inspect `git status --short` and `git log --oneline -8`; preserve any work owned by another session.
2. Confirm the public baseline with `https://pumpwizard.fun/api/public/health` before treating a UI report as a Raydium issue.
3. Re-read the Raydium plan and begin with the reproducible local fixture, not a production service.
4. Keep Pump.fun and Raydium identifiers, ledgers, data health, and UI labels isolated in every test.
5. Capture evidence for each gate before proposing the next rollout.
