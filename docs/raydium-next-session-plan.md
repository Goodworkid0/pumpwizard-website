# Raydium local enablement and test plan

## Goal

Turn the current Raydium CPMM observed-simulation pilot into a locally verified,
repeatable public-read candidate. This plan does not authorize a production
collector, a production trading route, or presentation of unavailable values as
Pump.fun data.

## Starting point

- The observed adapter records standard-RPC Raydium CPMM pool snapshots with
  source and account-identity evidence.
- The current narrow pilot has an isolated quote-token ledger and a public UI
  context, but deliberately limited activity and no USD valuation without
  separate FX evidence.
- Pump.fun remains the only live production simulation path.

## Session sequence

1. Establish a reproducible local fixture.
   - Select one CPMM pool and record bootstrap, pool, vault and AmmConfig account
     fixtures plus expected decoded identities.
   - Run archive and quote validation tests without a network dependency.
   - Keep the fixture pool ID, quote token, source endpoint and maximum snapshot
     age in a checked configuration contract.

2. Run the local observed collector in bounded mode.
   - Use the public RPC collector with an explicit cadence and a short fixed run
     window.
   - Confirm sequence continuity, content hashes, timestamp freshness and no
     duplicate observations.
   - Capture collector health, accepted/rejected snapshots and provider errors;
     do not substitute stale data for a fresh observation.

3. Exercise the isolated Raydium paper ledger.
   - Start a new quote-token virtual balance, never reuse a Pump.fun ledger.
   - Replay the captured snapshots through entry, hold, exit, stale-mark and
     restart cases.
   - Verify every ledger event against its observed snapshot sequence and run the
     ledger verifier after each scenario.

4. Publish a local read model only after evidence exists.
   - Add the observed ledger projection to the local public registry.
   - Preserve quote-token denomination and keep USD P&L unavailable unless an
     explicit, timestamp-matched FX evidence source is added and tested.
   - Ensure market selection never falls back to Pump.fun strategies, positions,
     metrics or history.

5. Validate the shared UI contract.
   - Test Live Data, Live Strategies, strategy detail, Lab Research, Downloads
     and Community with both `market=pumpfun` and `market=raydium-cpmm`.
   - Check direct links, page refresh, market switching from strategy details,
     unavailable/empty states, logos and market-aware labels.
   - Download the Raydium local trade/research data and verify its market ID,
     denomination, source references and event sequences.

6. Run failure and soak checks locally.
   - Cover stale RPC data, missing vault/account data, mismatched pool identity,
     duplicate sequence, restart recovery and a quote-gap while a position is
     open.
   - Run a bounded local soak and retain its summary, log hashes and validation
     receipt alongside the fixture set.

## Exit criteria for a later production proposal

Production consideration requires all of the following:

- Deterministic fixture, replay, ledger-verification and browser tests pass.
- A bounded local soak has no source-identity, sequence-continuity or
  cross-market leakage failures.
- Quote-token valuation and any USD conversion are explicitly labeled and
  separately evidenced.
- A deployment proposal shows a read-only/shadow rollout, health checks,
  rollback steps and no change to the Pump.fun collector or existing ledgers.

Until then, Raydium remains a local observed-simulation pilot.
