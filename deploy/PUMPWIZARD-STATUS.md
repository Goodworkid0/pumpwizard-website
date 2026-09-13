# pumpwizard.fun connection status

## Full app live — 2026-09-13 00:57:26 UTC

The landing has been replaced by the full application. See
[the release runbook](RELEASE-20260913.md). Code runs from
`/opt/pump-paper/releases/20260913-0105`; data is `/opt/pump-paper/data`.
The server is now the only active collector. All staged/final database hashes and
table counts were checked, public browser flows passed, and existing insidedb.ai
pages/health remained successful. The workstation source and frozen cutover snapshot
were retained. App-vhost rollback: `/opt/pump-paper/deploy/app-cutover-hij49o37/original.conf`.

Production backup and monitoring timers are enabled. The first full Telegram
backup/restore remains monitored separately. No PostgreSQL runtime cutover or
automatic backup deletion was performed. The historical landing notes below are
retained for reference and do not describe the current public homepage.

## Earlier landing setup

Brand update 2026-09-12: public landing uses option A (signal hat), the app's
#07090d background, #c4b5fd violet and #54e0b0 mint, and the message
"A little magic. A lot of research." Logo also supplies the favicon and social
preview image. Social links supplied by the user are active:
https://x.com/PumpWizard11 and https://t.me/PumpWizardFun.
The same URLs are recorded in local community.config.json for the full app.
Brand rollback backup: /opt/pump-paper/deploy/landing-brand-02m5yk4s.
New assets are copied into the running landing container, with their persistent
mount recorded in compose.landing.yml. No containers were recreated.

Updated 2026-09-12 19:08 UTC. The separate HTTPS vhost is active on idb-host and serves the
static landing page for pumpwizard.fun and www.pumpwizard.fun. HTTP requests to
either name redirect with 301 to https://pumpwizard.fun/. The main proxy
configuration remains unchanged. The existing homepage, www homepage and health
endpoint each returned HTTP 200 before and after the change.

Active origin config: /opt/pump-paper/deploy/pumpwizard.conf.
Running container copy: insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf.
Future recreation mount is recorded in /opt/insidedb/docker-compose.platform.yml.
Backup: /opt/pump-paper/deploy/landing-vhost-ligi_32u/compose.original.yml.

The main default.conf mount points at a stale inode. Do not use the older
prepare_landing_http.py script or append further routes to insidedb.conf. The
initial attempt was rolled back. For subsequent Pump Wizard updates, back up
pumpwizard.conf, validate it in combination with the current default.conf, update
the host file and running container copy, run nginx -t, then gracefully reload.
Do not recreate the shared proxy just to activate this domain.

The user changed nameservers. Both public names now respond through Cloudflare.
Let's Encrypt certificate /etc/letsencrypt/live/pumpwizard.fun/fullchain.pem
covers both names and expires 2026-12-11. Certbot uses the existing webroot
/opt/insidedb/nginx/www; certbot.timer is enabled. The deploy hook
/etc/letsencrypt/renewal-hooks/deploy/pumpwizard-nginx validates and gracefully
reloads insidedb-nginx when this certificate renews.

enable_landing_https.py installed HTTPS first, preserving HTTP while Cloudflare's
edge certificate activated. Its --redirect mode then installed the rendered
nginx-landing.conf.template and verified public requests with bounded redirect
following. The original shared default.conf and existing sites were preserved.
Latest rollback backup: /opt/pump-paper/deploy/landing-https-n0xokwsb/original.conf.

Verified from the origin: trusted HTTPS 200 for both landing pages and stylesheets,
HTTP 301 for both names, and existing insidedb.ai homepage/www/health checks 200.
Verified publicly: trusted HTTPS 200 on both names with Cloudflare headers,
HTTP 301 to the HTTPS apex, and expected landing content without redirect loops.
Cloudflare's dashboard encryption setting has not been inspected; Full (strict)
is supported by the installed valid origin certificate. DNSSEC and email settings
were not changed as part of this HTTPS work.
