# Hetzner + Cloudflare deployment

**Current deployment:** use [the 2026-09-13 release runbook](RELEASE-20260913.md).
The older commands below describe the initial setup, not the current cutover.
Do not append to the shared proxy configuration or run its old `--delete` sync
commands. The active release uses a separate vhost and verified frozen snapshots.

The production app runs as its own Compose project and joins the existing
`insidedb_insidedb-network`. It publishes no host port. The existing
`insidedb-nginx` container remains the only service listening on ports 80 and 443.

Replace `example.com` below with the apex domain. The origin IPv4 address is
`65.109.89.241`; the origin IPv6 address is `2a01:4f9:3051:442e::2`.

## 1. Prepare Cloudflare and Namecheap

1. Add the domain to Cloudflare and carefully copy any existing mail/TXT records.
2. If DNSSEC is enabled at Namecheap, disable it before changing nameservers.
3. At Namecheap, select Custom DNS and enter only the two nameservers assigned by
   Cloudflare.
4. In Cloudflare DNS, create an `A` record for `@` pointing to `65.109.89.241` and
   a `CNAME` record for `www` pointing to the apex. Keep both DNS-only until the
   origin certificate has been issued.

## 2. Stage the application

Do not start the server copy while the workstation collector is still writing the
same ledgers. First stage the code and most data while the local process remains
online. The final synchronization happens during cutover.

```bash
rsync -az --delete --exclude data/ --exclude .git/ \
  /home/c/Desktop/projekti/pump-agent/ idb-host:/opt/pump-paper/
rsync -az --exclude service.env \
  /home/c/Desktop/projekti/pump-agent/data/ idb-host:/opt/pump-paper/data/
```

If provider credentials are needed, create `/opt/pump-paper/data/service.env`
directly on the server with mode `0600`. Never copy it through source control.

## 3. Add the HTTP vhost and issue the certificate

Render `deploy/nginx-http.conf.template` with the real domain and append it to
`/opt/insidedb/nginx/insidedb.conf`. Test and reload Nginx:

```bash
docker exec insidedb-nginx nginx -t
docker exec insidedb-nginx nginx -s reload
```

Issue a Let's Encrypt certificate using the webroot already mounted in Nginx:

```bash
certbot certonly --webroot -w /opt/insidedb/nginx/www \
  -d example.com -d www.example.com
```

Replace the temporary HTTP vhost with the rendered
`deploy/nginx-https.conf.template`, then test and reload Nginx again.

## 4. Cut over the collector

Stop the workstation service so SQLite is consistent and there is only one live
collector. Repeat the data sync, fix ownership for the non-root container user,
then start the production app:

```bash
systemctl --user stop pump-agent.service
rsync -az --delete --exclude service.env \
  /home/c/Desktop/projekti/pump-agent/data/ idb-host:/opt/pump-paper/data/
ssh idb-host 'chown -R 10001:10001 /opt/pump-paper/data && cd /opt/pump-paper/deploy && docker compose -f compose.production.yml up -d --build'
```

Check the container internally before enabling the Cloudflare proxy:

```bash
ssh idb-host "docker exec pump-paper python3 -c \"import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/api/public/health', headers={'Host':'127.0.0.1:8765'}), timeout=10).status)\""
curl --resolve example.com:443:65.109.89.241 https://example.com/api/public/health
```

If cutover fails, stop the server container and restart the workstation service.
The final sync preserves the pre-cutover ledgers.

## 5. Enable Cloudflare

Set the apex and `www` records to Proxied. In SSL/TLS, use Full (strict). Enable
Always Use HTTPS. After validation, enable Cloudflare DNSSEC and add the generated
DS record at Namecheap.

The Nginx template allowlists public routes, limits requests/connections and blocks
operator APIs and writes. The app also denies these independently. The application
container exposes no host port. Configure trusted Cloudflare real-IP handling
before proxying so rate limits do not aggregate visitors behind an edge IP.
Read [launch operations](../docs/launch-operations.md) before deploying; the new
template still requires `nginx -t` and origin-level verification.

## Operations

```bash
cd /opt/pump-paper/deploy
docker compose -f compose.production.yml ps
docker compose -f compose.production.yml logs --tail=100 app
docker compose -f compose.production.yml up -d --build
docker compose -f compose.production.yml restart app
```

Back up the complete `/opt/pump-paper/data` directory while the app is stopped.
The SQLite database, WAL, and SHM files form one live state and must be kept
together.

For integrity-checked standalone copies instead, use `scripts/backup_lab.py` from
the host checkout. It includes committed WAL records through SQLite's backup API,
and supports a restore into a fresh directory. Online copies are not atomic across
ledgers. Final cutover backups still require a coordinated writer stop. The
read-only monitoring probe and optional timer are described in
[launch operations](../docs/launch-operations.md); external backup and alert
destinations are not configured by the templates.
