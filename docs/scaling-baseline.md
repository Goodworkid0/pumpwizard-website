# Measured scale preparation

`scripts/benchmark_scale.py` runs real ResearchRunner ledgers with SQLite FULL
synchronous commits, one shared feature archive, public report publication, and
four concurrent in-process public readers. Every database is disposable synthetic
data. The command does not connect to or reconfigure production, use providers,
start PostgreSQL, or enable new funded instances.

```bash
# This shell-only limit matches the deployed app's existing descriptor allowance.
ulimit -n 8192
python3 scripts/benchmark_scale.py --instances 10,100,1000 --steps 15 --coins 4 \
  --output /absolute/new-capacity-report.json
```

The output must be new. Completed scenarios and failed-scenario diagnostics are
retained, including descriptor-limit failures. Sizes are bounded to 1,000
instances, 100 cycles and 50 coins per cycle, with a per-scenario time budget.

## Local results — 2026-09-13

Python 3.11.2, SQLite 3.40.1, Linux, 16 visible CPUs. Fifteen synthetic cycles,
four coins per cycle, ten repeated research signal families. Raw results:
[20260913-local.json](benchmarks/20260913-local.json).

| Instances | Cycle p95 | Publication max (4 samples) | Concurrent read p95 | Read errors |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 38 ms | 28 ms | 2.4 ms | 0 |
| 100 | 331 ms | 255 ms | 3.9 ms | 0 |
| 1,000 | 4,057 ms | 2,864 ms | 17.3 ms | 0 |

The 1,000-instance run produced 1,500 synthetic closed positions, verified state
after reopening the ledgers, and checked one representative SQLite backup/restore.
Its working set grew about 338 MB including WAL/page allocation; this is **not**
a daily storage forecast. Process peak RSS was about 279 MiB (high-water mark
across scenarios). The initial attempt hit the workstation's descriptor allowance;
rerunning with 8,192 succeeded without changing any global/production setting.

## What this supports—and does not

This establishes that the current runner/read-model path can handle this small,
short-history synthetic workload at 1,000 instances on the development machine.
It does not establish production capacity: real market ingestion, ownership
requests, coin-memory work, long histories, HTTP/TLS/proxy latency, backup traffic
and shared-host contention are excluded. Publication is timed between cycles;
public reads overlap simulator writes, not continuous report rebuilding.
The representative restore is not an off-site or whole-application recovery drill.

There is no measured justification here for an immediate live database switch.
Do not turn on 1,000 production instances based on this report.

## Next gates before expanding production

1. Rehearse with a frozen real-history copy on a production-like isolated host.
   Measure full ingestion/cycle time, publication age, public HTTP p95/p99,
   descriptor use, peak memory, daily committed-data growth and backup duration.
2. Run a sustained workload with a slow provider, failed publication, concurrent
   exports and backup I/O. Define budgets and a recovery procedure in advance;
   the current 30-second cycle/publication checks are screening gates, not an SLA.
3. Move public serving/report building into a separate process when contention
   or long-history measurements justify it. Keep a versioned read-model boundary;
   a public request must never acquire the simulator's writer connection.
4. Rehearse the existing PostgreSQL staging importer on the frozen archive.
   Design the execution adapter around atomic state/events/positions/equity plus
   idempotent input IDs, not a mechanical SQL placeholder substitution. Shared
   inputs must commit before dependent instance transactions.
5. Shadow replay the SQLite and future PostgreSQL adapters on identical recorded
   inputs. Require equal balances, decisions, fees, sale legs, closed positions,
   crash-recovery behavior and source-reference coverage before an explicit cutover.

This benchmark itself does not measure the subsequently added PostgreSQL adapter
or separate public worker, and no dual writes are used. See [PostgreSQL runtime and staging](postgres-migration.md) and
[public read isolation](public-read-layer.md). The failing production Telegram
backup/restore gate remains independent and must be resolved before broad scale-up.
