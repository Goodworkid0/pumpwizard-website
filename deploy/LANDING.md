# Preparation landing page

Start only this static service on Hetzner:

```sh
cd /opt/pump-paper/deploy
docker compose -f compose.landing.yml up -d
docker exec insidedb-nginx wget -q -O - http://pump-paper-landing:8080/healthz
```

It has no host ports, application code, credentials or database mounts. The
existing Nginx proxy can reach it through the Docker network. The full collector
remains stopped on Hetzner.

The landing page is live at https://pumpwizard.fun, including www, with HTTP
redirects and an automatically renewed origin certificate. See
PUMPWIZARD-STATUS.md for current configuration paths, backups and verification.
The shared proxy uses a separate Pump Wizard vhost; preserve mounted file inodes
when updating it and validate the combined configuration before reloading.

The landing uses logo option A from web/assets/brand, copied unchanged into
landing/assets/pumpwizard-logo.png. The image is used in the header, hero,
favicon and sharing metadata. The static server's CSP allows local styles and
images. The social buttons link to https://x.com/PumpWizard11 and
https://t.me/PumpWizardFun, opening in new tabs with noopener noreferrer.

For static updates, back up the landing files, preserve mounted file inodes,
validate Nginx and reload the landing container if nginx.conf changed. The assets
directory is copied into the current container and recorded as a Compose mount
for future recreation. Check both public hostnames, redirects, stylesheets and
images, plus desktop/mobile rendering and the existing shared-proxy sites.

When the project is ready, migrate and check its data separately using the
deployment runbook. Keep this landing container available, validate the app
internally, then replace only this domain's vhost with the production template.
Keep all public methods except GET/HEAD blocked until authenticated administration
is implemented. The domain and certificate stay the same. Test and reload Nginx,
then verify the actual site through Cloudflare. Restoring the landing vhost is
the immediate website rollback; rolling back collector data is a separate task.
