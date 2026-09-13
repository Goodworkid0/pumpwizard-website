# Public strategy experiment

The former short-term cadence strategy was retired on 11 September 2026. Its
ledger, the earlier long-hold ledgers, account allocation and coin memory were
archived under `experiments/public-reset-20260911-1755/data/`, rather than
deleted. The new public site starts with no historical trades.

Three independent paper-only strategies are active. Each begins with $500 of
virtual capital and has a separate immutable SQLite ledger:

| Strategy | Entry band | Signal window | Capacity |
|---|---:|---:|---:|
| Early frequency | $8k–$15k | 30 seconds | 20 positions |
| Core frequency | $10k–$25k | 30 seconds | 20 positions |
| Mid-cap patient | $25k–$50k | 45 seconds | 15 positions |

The settings are in `long_hold_experiments.json`. The public website is `/`;
the local administration control plane is `/acp`. ACP accepts a complete
`early-ladder-v1` strategy object and validates it before starting a separate
ledger. Existing strategy configurations never change in place.

The wider bands, shorter signal windows and higher capacities are designed to
create substantially more *eligible opportunities*. A target of 100–200 daily
fills cannot be guaranteed without bypassing the ownership, liquidity and
execution-cost screens, which this experiment does not do. Counts and P&L are
displayed per strategy, never merged into one misleading portfolio total.

The server is currently loopback-only. Before making the website genuinely
internet-public, choose a hosting/domain and an authentication boundary for ACP.
