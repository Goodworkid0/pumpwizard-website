#!/usr/bin/env python3
"""Gracefully stop this project's paper collector for one exact database."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--db', type=Path, default=root / 'data' / 'cadence.sqlite')
args = parser.parse_args()
target = args.db.resolve()
found = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmd = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if len(cmd) < 3 or cmd[2] != 'live':
            continue
        cwd = (proc / 'cwd').resolve()
        script = Path(cmd[1])
        if not script.is_absolute():
            script = cwd / script
        if script.resolve() != root / 'paper_trader.py':
            continue
        db = Path(cmd[cmd.index('--db') + 1]) if '--db' in cmd else root / 'data' / 'cadence.sqlite'
        if not db.is_absolute():
            db = cwd / db
        if db.resolve() == target:
            found.append(int(proc.name))
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        continue
if not found:
    raise SystemExit('No matching visible collector process. If it is still running, check process permissions.')
if len(found) != 1:
    raise SystemExit('More than one matching process found; no signals sent.')
pid = found[0]
# A direct SIGTERM to a supervised process would trigger its automatic restart.
# The PID above already matches this project's script and the exact database.
cgroups = (Path('/proc') / str(pid) / 'cgroup').read_text().splitlines()
if any(line.rsplit('/', 1)[-1] == 'pump-agent.service' for line in cgroups):
    subprocess.run(['systemctl', '--user', 'stop', 'pump-agent.service'],
                   check=True, timeout=100)
    print(f'Paper collector service stopped; database preserved: {target}')
    raise SystemExit(0)
os.kill(pid, signal.SIGTERM)
for _ in range(150):
    proc = Path('/proc') / str(pid)
    try:
        stopped = not proc.exists() or b'Z' in (proc / 'stat').read_bytes().split()[2:3]
    except FileNotFoundError:
        stopped = True
    if stopped:
        print(f'Paper collector stopped gracefully: PID {pid}; database preserved: {target}')
        break
    time.sleep(0.1)
else:
    raise SystemExit('SIGTERM sent; collector is still stopping. No forced kill was used.')
