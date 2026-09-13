# Standalone PumpWizard Telegram backups

**Current operational status (2026-09-13): paused by operator request.** Production
timer disabled, service stopped, and `backup_enabled: false` set in both local and
production private configuration. Daily/smoke/upload commands skip new jobs while
paused. Site and disk monitoring stay enabled; backup-specific checks explicitly
report `disabled_by_operator`. Existing recovery files and Telegram backups remain.

The persisted pause is set by `scripts/pause_telegram_backups.py --apply`, after
stopping the relevant timer/service. It retains credential paths and backup data.
Resuming requires an explicit configuration change and re-enabling the timer; no
automatic expiry is set. Restore and probe commands remain available for deliberate
recovery work. See [storage rework plan](storage-rework-plan.md) before rescheduling.

PumpWizard connects directly to Telegram, without the OTS server, HTTP API, login,
upload database, content key or session. Transport and bounded encryption patterns
adapt MIT-licensed OTS code; see third_party/OpenTelegramStorage-LICENSE.txt.
The older scripts/ots_backup.py adapter is retained but inactive.

## Setup and recovery secrets

The one-time scripts/import_telegram_backup.py --container ots --apply command
reads only the existing API ID/hash, bot token and destination channel from the
container. Secrets use a captured subprocess pipe, never printed output or command
arguments. OTS settings are read-only and its files/session remain untouched.
The importer refuses to overwrite an existing configuration.

Private files live in data/telegram-backup/ (directory 0700, files 0600):

- config.json: paths, part size, disk floor and notifications.
- credentials.json: Telegram API credentials, bot token and channel ID.
- key.json: a newly generated independent 256-bit encryption key.
- session.json: an independent bot MTProto session, created on first connection.

**Save key.json and credentials.json independently in a secure offline location
or password manager.** These are intentionally excluded from backups. Losing the
key makes backups unrecoverable; the OTS key cannot decrypt them. The session can
be recreated. Retain this source code and channel/manifest message IDs separately.

Storage root: /home/c/Desktop/projekti/pump-agent-backups, with snapshots/ and jobs/.
Runtime: Python 3.11+ in a dedicated .venv-backup, with Telethon, cryptography and
native cryptg acceleration. The service does not change the live application's
Python packages. Recreate the tested runtime with:

```bash
python3 -m venv .venv-backup
.venv-backup/bin/python -m pip install -r requirements-backup.lock.txt
```

Use .venv-backup/bin/python instead of python3 for the backup commands below.
Large parts use four bounded MTProto upload connections by default; configuration
upload_connections accepts up to eight. Setting it to one uses the ordinary
uploader. RPC failures, including throttling, stop the run rather than spawning
unlimited connections or silently switching transport. Byte retries are separate
from durable document-message creation. See the pinned Telethon dependency before
changing its private sender APIs.

```bash
python3 scripts/telegram_backup.py probe
python3 scripts/telegram_backup.py smoke --apply
python3 scripts/telegram_backup.py daily --apply
```

Probe sends no messages. Smoke uploads a synthetic multipart SQLite snapshot,
downloads/decrypts/restores it and recovers the channel manifest. Smoke receipts
never count as production backup freshness. Daily backs up the real lab.

With restore_first_backup enabled (as on this host), the first verified production
backup is also recovered through its encrypted Telegram manifest into a new
production-restore-* directory. Only a complete successful restore writes
production-drill.json. Later runs retain that one-time drill result; it is not
evidence that every subsequent backup was fully restored. A failed drill can be
retried from the completed job without making another snapshot. Interrupted
restore output is retained, and a retry uses a new destination.

## Protection and consistency

The allowlist is top-level data/*.sqlite and the configuration names in
scripts/backup_lab.py. Nested credential/session files, environment files, source
code, logs and arbitrary directory contents are excluded. SQLite's backup API
captures committed WAL data. Each database gets an online snapshot, **not one
cross-database atomic point in time**. Stop all writers for an atomic cutover.

Snapshots pass SHA-256, SQLite quick_check and table-count checks. A gzip archive
is checked against its source before upload. Default 128 MiB parts use bounded
AES-256-GCM blocks, random per-part salt, HKDF-SHA256 and authenticated run/part/block
identity. Only ciphertext is uploaded. An encrypted recovery manifest records
member hashes, part order, message IDs and key ID.

Every part is downloaded and decrypted. Part hashes and the whole archive hash
must match before saving a verified receipt; the manifest is also read back.
The restore command additionally extracts and checks the databases again.

## Local retention

The approved `local_retention: latest_verified` policy keeps one latest local
recovery copy and every encrypted Telegram backup. It requires a verified first
recovery drill and rechecks the retained copy's file hashes, SQLite integrity,
table counts and manifest against its Telegram receipt before any cleanup.
Only exact, hash-matching older/duplicate local backup payloads are deleted.
Receipts, recovery metadata, retired manifests, live ledgers, credentials and
Telegram messages are never deleted. Interrupted or unidentified restores are
not automatically pruned. Changed files or unsafe paths stop cleanup.

Preview with `scripts/retain_backups.py --enable`; apply and persist the policy
with `--enable --apply`. The command shares the backup operation lock. Daily
backups perform cleanup before new preparation and after verified completion.
Cleanup is repeatable after interruption. Removed copies can be recovered from
Telegram using the independent content key and credentials. The first-drill
marker remains historical evidence; its old extraction may be retired after a
newer verified snapshot is retained. `retention-last.json` identifies the current
local recovery copy. Keep recovery credentials off-host.

Fresh Telegram checks require a private channel with auto-delete off. This client
does not consume updates, change bot metadata or use OTS caption markers.
Existing OTS data is never modified or deleted.

## Schedule and monitoring

The user service runs at low priority. The timer schedules production backups
daily at **05:00 Europe/Belgrade**, with up to 15 minutes of jitter and catch-up
after downtime. Installing source alone does not enable it.

```bash
systemctl --user link /home/c/Desktop/projekti/pump-agent/deploy/pump-telegram-backup.service /home/c/Desktop/projekti/pump-agent/deploy/pump-telegram-backup.timer
systemctl --user daemon-reload
systemctl --user enable --now pump-telegram-backup.timer
systemctl --user start --no-block pump-telegram-backup.service
systemctl --user list-timers pump-telegram-backup.timer
journalctl --user -u pump-telegram-backup.service -n 30 --no-pager
```

The user manager must survive logout (check loginctl show-user c -p Linger).
Only one process may use this configuration/session. Daily runs resume the newest
unfinished production transfer. Acknowledged messages are retained and reverified.
Lost send acknowledgements stop for manual reconciliation with the labelled channel
message, rather than silently duplicating a post. Do not blindly clear checkpoints.
Preparation failures may leave partial local directories for inspection.

Success and failure notifications use the same private channel, without secrets,
database contents or raw provider errors. Notification failure returns a nonzero
exit even if backup verification succeeded; a verified receipt remains valid.
Host/network outages can prevent alerts: independent machine-down monitoring is
still needed.

```bash
python3 scripts/check_lab.py --backup-root /home/c/Desktop/projekti/pump-agent-backups/snapshots --telegram-jobs /home/c/Desktop/projekti/pump-agent-backups/jobs --max-backup-age 129600
```

The remote check ages the production **source snapshot**, not the upload date.
It reads local receipts, not a fresh Telegram download. The 36-hour threshold
allows for the daily schedule and transfer time. Invoke from your monitoring
system; the backup timer alone does not poll health.

On this host pump-lab-check.timer is enabled and runs scripts/monitor_lab.py every
two minutes. It checks readiness, local/remote backup freshness, disk space,
transfer progress (30-minute stale threshold), and the first production restore
marker. Three consecutive identical failing check sets trigger a private Telegram
alert, with hourly reminders and one recovery notification. Progress logs show
bytes transferred every 30 seconds. A pending first backup/restore is deliberately
reported as attention, not successful recovery. Alerts use Bot API calls only and
do not contend for the active MTProto transfer session.

```bash
.venv-backup/bin/python scripts/monitor_lab.py
systemctl --user list-timers pump-lab-check.timer
journalctl --user -u pump-lab-check.service -n 30 --no-pager
```

The read-only command sends no notifications without --notify. An offline host
cannot report its own outage; external machine-down monitoring remains necessary.

## Restore into a new directory

With a surviving receipt:

```bash
python3 scripts/telegram_backup.py restore --receipt /backups/jobs/RUN/telegram-receipt.json --destination /restores/drill-001
```

If local jobs are lost, locate the channel document whose caption contains
"pwbackup":1 and "kind":"manifest". Configure the original key and credentials
on the recovery host, then:

```bash
python3 scripts/telegram_backup.py recover-manifest --message-id 12345 --destination /restores/manifest-001
python3 scripts/telegram_backup.py restore --receipt /restores/manifest-001/telegram-receipt.json --destination /restores/drill-001
```

Replace example paths/message ID. Parents must exist; destination directories must
be new. Restore checks ciphertext, plaintext, archive members, SQLite integrity
and row counts. Unexpected paths, links, duplicates, truncation and hash mismatches
fail closed. Partial output remains for inspection. Nothing overwrites the live lab
or starts a collector. Full application recovery/accounting reconciliation are
separate release drills.

## Retention and limits

No automatic deletion, incremental backup or deduplication is implemented. Full
snapshots and compressed archives accumulate locally. Space checks reserve estimated
snapshot/archive capacity plus a 10%-of-disk floor and initial restore space;
concurrent processes can still fill the disk. Monitor
capacity and approve retention before leaving full backups unattended indefinitely.

Ordinary commands have a twelve-hour async deadline. The first production backup
plus independent full restore has a twenty-four-hour budget; systemd imposes a
twenty-six-hour hard deadline. A timer firing while the same oneshot service is
still running does not start a concurrent backup.
Blocking snapshot work is also bounded per database. Telegram throttling, revoked
bot access, removed messages and account loss can interrupt recovery. Telegram is
an extra encrypted off-site copy, not immutable or guaranteed storage. Keep local
copies, save the key independently, and regularly rehearse a production restore.
