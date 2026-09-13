#!/usr/bin/env python3
"""Replay saved long-hold inputs under fixed alternative configurations.

This is a counterfactual research tool.  It never opens the live ledger for
writing or contacts a market/RPC provider.  A result is in-sample: it describes
what this exact, short recorded sample would have done, not expected returns.
"""
import argparse
import copy
import json
import sqlite3
import tempfile
import zlib
from pathlib import Path

from paper.ladder import LadderRunner


def changed(config, **values):
    result = copy.deepcopy(config)
    result.update(values)
    return result


def presets(config):
    """Exit and entry alternatives worth comparing against the active rules."""
    return {
        "active": config,
        "earlier_targets_25_50_80": changed(config, targets=[
            {"market_cap_usd": 25_000, "initial_fraction": .40},
            {"market_cap_usd": 50_000, "initial_fraction": .30},
            {"market_cap_usd": 80_000, "initial_fraction": .20},
        ]),
        "front_loaded_30_60_100": changed(config, targets=[
            {"market_cap_usd": 30_000, "initial_fraction": .60},
            {"market_cap_usd": 60_000, "initial_fraction": .25},
            {"market_cap_usd": 100_000, "initial_fraction": .10},
        ]),
        "delayed_targets_40_80_120": changed(config, targets=[
            {"market_cap_usd": 40_000, "initial_fraction": .40},
            {"market_cap_usd": 80_000, "initial_fraction": .30},
            {"market_cap_usd": 120_000, "initial_fraction": .20},
        ]),
        "trailing_drawdown_25pct": changed(config, trailing_drawdown_fraction=.25),
        "trailing_drawdown_50pct": changed(config, trailing_drawdown_fraction=.50),
        "stop_loss_20pct": changed(config, stop_loss_fraction=-.20),
        "stop_loss_40pct": changed(config, stop_loss_fraction=-.40),
        "growth_2pct_reserve_20": changed(
            config, minimum_growth_fraction=.02, minimum_reserve_growth_usd=20),
        "growth_3pct_reserve_30": changed(
            config, minimum_growth_fraction=.03, minimum_reserve_growth_usd=30),
        "entry_band_10k_15k": changed(config, entry_cap_max=15_000),
        "entry_band_10k_11k": changed(config, entry_cap_max=11_000),
    }


def input_batches(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [json.loads(zlib.decompress(payload))
                for _, payload in db.execute("SELECT at,payload FROM inputs ORDER BY at")]
    finally:
        db.close()


def replay(name, config, ownership_config, batches, temporary_directory):
    # _step is the deterministic strategy transition.  Avoiding its persistence
    # wrapper makes a sweep quick, while sales/trades still use a throwaway DB.
    runner = LadderRunner(temporary_directory / f"{name}.sqlite", config, ownership_config)
    runner.event = lambda *args, **kwargs: None
    try:
        with runner.db:
            for batch in batches:
                runner._step(batch)
        state = runner.state
        completed = [json.loads(row[0]) for row in runner.db.execute("SELECT data FROM trades")]
        unrealized = sum(position["mark_value_usd"] - position["remaining_cost_usd"]
                         for position in state["positions"].values())
        net = state["realized_pnl_usd"] + unrealized - state["failed_fees_usd"]
        return {
            "preset": name,
            "entries": len(state["attempted"]),
            "completed_positions": len(completed),
            "open_positions": len(state["positions"]),
            "realized_pnl_usd": round(state["realized_pnl_usd"], 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "failed_attempt_fees_usd": round(state["failed_fees_usd"], 2),
            "net_pnl_usd": round(net, 2),
            "win_count": sum(trade["pnl_usd"] > 0 for trade in completed),
            "loss_count": sum(trade["pnl_usd"] < 0 for trade in completed),
        }
    finally:
        runner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path("data/ladder.sqlite"))
    parser.add_argument("--config", type=Path, default=Path("ladder.config.json"))
    parser.add_argument("--ownership-config", type=Path, default=Path("ownership.config.json"))
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    ownership_config = json.loads(args.ownership_config.read_text())
    batches = input_batches(args.ledger)
    with tempfile.TemporaryDirectory(prefix="pump-long-hold-sweep-") as directory:
        directory = Path(directory)
        results = [replay(name, value, ownership_config, batches, directory)
                   for name, value in presets(config).items()]
    results.sort(key=lambda result: result["net_pnl_usd"], reverse=True)
    output = {
        "input_ledger": str(args.ledger),
        "batches": len(batches),
        "results": results,
        "warning": ("In-sample counterfactuals only. Configuration selection from these "
                    "results would be overfitting; do not change a live ledger in place."),
    }
    text = json.dumps(output, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
