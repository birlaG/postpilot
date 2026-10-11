# Deploying to a Contabo VPS

These steps assume a fresh Contabo VPS running Ubuntu 24.04 LTS, a domain you control, and
the repository checked out at `/opt/postpilot`. Run server commands as `root` (or prefix
them with `sudo`). Do not send `.env` or private keys in chat or commit them to Git.

## 1. Point your domain at the VPS

In your DNS provider, create `A` records for `yourdomain.com` and `www.yourdomain.com`
pointing to the VPS IPv4 address. Add `AAAA` records only if IPv6 is configured and
reachable on the VPS. Wait until the records resolve before requesting HTTPS certificates.

## 2. Update the server and install Docker

```bash
apt update && apt -y upgrade
apt install -y docker.io docker-compose-v2 ufw git openssl
systemctl enable --now docker
docker --version
docker compose version
```

If Ubuntu cannot find `docker-compose-v2`, use Contabo's Ubuntu 24.04 image or install
Docker Engine and the Compose plugin from Docker's official Ubuntu repository.

Open only SSH and web traffic in the VPS firewall:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status
```

Also check the Contabo firewall/security settings in the control panel, if enabled, and
allow inbound TCP ports 22, 80, and 443 there. Keep your current SSH session open while
enabling the firewall so you can recover if SSH access is blocked.

## 3. Get the code and create production secrets

Use your repository URL; for a private repository, configure read-only SSH deploy-key
access for this VPS before cloning.

```bash
git clone <repository-url> /opt/postpilot
cd /opt/postpilot
cp .env.example .env
chmod 600 .env
```

Generate independent Django and database secrets. Put each output into its matching
variable in `.env` (`DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`):

```bash
openssl rand -hex 48
openssl rand -hex 32
```

Set `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com`,
`DJANGO_CSRF_TRUSTED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com`,
`PUBLIC_BASE_URL=https://yourdomain.com`, `POSTGRES_HOST=db`, and `REDIS_URL=redis://redis:6379/0`.
Keep the database name and user as `postpilot` unless you also update the backup commands.

Build the app image, then generate a Fernet encryption key for `FERNET_KEY`. Save it
somewhere secure as well as in `.env`: losing it makes saved social-platform tokens
unreadable.

```bash
docker compose build web
docker compose run --rm --no-deps web python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
nano .env
```

Copy the printed key into `FERNET_KEY`. Fill in the third-party API credentials as you
create each provider/payment app; those integrations can be added after the site is up.
Never leave the example Django secret or database password in `.env`.

Validate Compose configuration without printing its resolved secrets:

```bash
docker compose config -q
```

## 4. Start temporarily over HTTP

The temporary config serves the ACME challenge and proxies the health endpoint. Do not
use this HTTP-only state for accounts or real user data.

```bash
docker compose -f docker-compose.yml -f deploy/compose.http.yml up -d --build
docker compose ps
curl -fsS http://yourdomain.com/health/
```

The health request should return `ok`. If it does not, inspect
`docker compose logs --tail=100 web nginx db` before continuing.

## 5. Obtain the HTTPS certificate

Replace the email and domain values below with the address and DNS name you control:

```bash
docker compose run --rm certbot certonly --webroot -w /var/www/certbot \
  -d yourdomain.com -d www.yourdomain.com \
  --email you@example.com --agree-tos --no-eff-email
```

Edit `deploy/nginx.conf` and replace every `yourdomain.com` with your real domain. Confirm
the certificate paths use the same primary domain passed to Certbot.

Switch from the HTTP bootstrap configuration to the HTTPS configuration and recreate Nginx:

```bash
docker compose up -d --force-recreate nginx
curl -fsS https://yourdomain.com/health/
```

The normal Compose command uses `deploy/nginx.conf`; do not keep passing the HTTP override
after this step.

Schedule certificate renewal. Open root's crontab with `crontab -e` and add:

```cron
17 3 * * * cd /opt/postpilot && /usr/bin/docker compose run --rm certbot renew --quiet && /usr/bin/docker compose exec -T nginx nginx -s reload >> /var/log/postpilot-certbot.log 2>&1
```

The certbot service shares the certificate and challenge volumes with Nginx. Renewal checks
those existing certificates and Nginx reloads to use renewed files.

## 6. Create the admin account and configure the app

```bash
docker compose exec web python manage.py createsuperuser
```

Open `https://yourdomain.com/admin/`. Configure plans and payment-provider IDs there. Set
up OAuth redirect URLs and payment webhooks only after their provider apps are created; the
URLs and provider notes are below.

## 7. Set up database backups

Create a private backup directory:

```bash
install -d -m 700 /var/backups/postpilot
```

Add this nightly job to root's crontab. The script checks the gzip archive before replacing
the day's backup and removes incomplete temporary files on errors.

```cron
0 2 * * * /bin/bash /opt/postpilot/deploy/backup-db.sh >> /var/log/postpilot-backup.log 2>&1
```

Run `/bin/bash /opt/postpilot/deploy/backup-db.sh` once manually and confirm the archive is
created. Periodically test a restore on a separate test database/server before relying on
backups. Back up the persistent `postpilot_media` Docker volume as well; it contains
uploads/generated images. Keep encrypted copies of database and media backups off the VPS.

## 8. Verify and update

Useful checks:

```bash
docker compose ps
docker compose logs --tail=100 web worker-generate worker-publish beat nginx
curl -fsS https://yourdomain.com/health/
```

To deploy a later Git update:

```bash
cd /opt/postpilot
git pull
docker compose up -d --build
docker compose ps
```

Database migrations run when the web container starts. Take a database backup before
deploying changes that include migrations.

Scaling later: move `worker-generate` and `worker-publish` to another VPS configured with
private access to the same Redis and PostgreSQL services; do not expose those services to
the public internet. Scale a worker with `docker compose up -d --scale worker-generate=3`.

## Platform developer apps (needed before users can connect)

Create one app per platform. Each redirect URI is `https://yourdomain.com/oauth/<provider>/callback/`.
Approvals take days to weeks, so start now. Until an app is approved, only you and your listed test
users can connect; that is enough to test the whole flow.

| Platform | Provider in URL | Console | What to request | Notes |
| --- | --- | --- | --- | --- |
| YouTube | `google` | Google Cloud Console: enable YouTube Data API v3, OAuth consent screen, Web client | scopes `youtube.upload`, `youtube.readonly` | Uploads from an **unaudited** project are forced to private. Apply for the YouTube API compliance audit. Default quota is 10,000 units/day and an upload costs about 1,600. |
| Facebook + Instagram | `meta` | developers.facebook.com, Business app, Facebook Login | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish` | Needs Meta App Review and business verification for public use. Instagram must be a Business/Creator account linked to a Page. |
| X | `x` | developer.x.com, OAuth 2.0 app (confidential client) | `tweet.read tweet.write users.read media.write offline.access` | Posting needs a paid API tier. Check its monthly post cap against your plan sizes. |
| LinkedIn | `linkedin` | linkedin.com/developers, add "Share on LinkedIn" and "Sign In with OpenID Connect" products | `openid profile w_member_social` | Personal profiles only. Company Pages need the Community Management API (separate review). Access tokens last about 60 days and are not refreshable for standard apps, so users must reconnect; the app marks the account "expiring". |

## Payments

- **Stripe** (USD): create three monthly recurring prices, paste ids into each Plan's `stripe_price_id`,
  add the webhook above, set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`.
- **Razorpay** (INR primary): create three monthly plans, paste ids into `razorpay_plan_id`, add the webhook.
  If Razorpay credentials are absent, Cashfree can provide INR recurring autopay instead. Create a Cashfree
  account with Subscriptions enabled, copy its API client ID and secret into `CASHFREE_CLIENT_ID` and
  `CASHFREE_CLIENT_SECRET`, set `CASHFREE_ENVIRONMENT=sandbox` while testing, and switch it to `production`
  only after end-to-end sandbox verification. `CASHFREE_API_VERSION` defaults to `2026-01-01`.
  The app creates a monthly periodic mandate for the selected INR plan and launches authorization with
  Cashfree's hosted JavaScript checkout. Customers enter a 10-digit Indian mobile number during checkout;
  this number is sent to Cashfree and is not stored by PostPilot. Cashfree's webhook URL is
  `https://yourdomain.com/billing/webhook/cashfree/`; configure `SUBSCRIPTION_STATUS_CHANGED`,
  `SUBSCRIPTION_AUTH_STATUS`, `SUBSCRIPTION_PAYMENT_SUCCESS`, and `SUBSCRIPTION_PAYMENT_FAILED`.
  Cashfree validates webhooks with `CASHFREE_CLIENT_SECRET`. If both gateways are configured, Razorpay
  remains primary. Cashfree checkout requires recurring subscriptions and does not fall back to one-time
  payment or manual renewal.
- Referral wallet defaults are `$0.50` for a referred customer's first successful USD payment and
  `₹50` for their first successful INR payment. Configure the amounts in cents/paise with
  `REFERRAL_REWARD_USD_CENTS` and `REFERRAL_REWARD_INR_PAISE`, and set `REFERRAL_MAX_REWARDS`
  for the per-referrer cap. Ensure Stripe sends `invoice.paid`, Razorpay sends
  `subscription.charged`, and Cashfree sends `SUBSCRIPTION_PAYMENT_SUCCESS`; only confirmed
  payments earn rewards.
- Wallet credit is promotional and non-withdrawable. It stays in its earned currency and can be
  used once during a new subscription checkout; it does not discount automatic renewals. Stripe
  discounts are created for the checkout. For INR, create a one-time ₹50 subscription offer in
  Razorpay, put its `offer_id` in `RAZORPAY_REFERRAL_OFFER_ID`, and make sure the offer is valid
  for API-created subscriptions. Razorpay's offer is a fixed ₹50, so a checkout redeems one ₹50
  reward; remaining balance stays in the wallet. Active subscriptions must be cancelled with the
  provider before a customer can start a new checkout with wallet credit, to avoid duplicate
  subscriptions. Previously earned monthly-post referral bonuses remain unchanged.
- INR wallet redemption currently requires the Razorpay offer and is intentionally unavailable through
  Cashfree; Cashfree subscription amounts are not reduced because that would also alter future renewals.
- Prices in `seed_plans` (USD 9 / 29 / 99, INR 599 / 1999 / 7499) are proposals. Edit them in `/admin/`
  and make sure the Stripe/Razorpay prices match.
