# Deploying to a Contabo (Ubuntu) VPS

1. **Server basics**
   ```bash
   apt update && apt install -y docker.io docker-compose-plugin ufw
   ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
   ```
2. **Code and config**
   ```bash
   git clone <your repo> /opt/postpilot && cd /opt/postpilot
   cp .env.example .env && nano .env     # fill every value; set DJANGO_DEBUG=0
   ```
3. **First start (HTTP only)**: in `deploy/nginx.conf` comment out the `443` server block and the
   `301` redirect, then `docker compose up -d --build`.
4. **Certificate**
   ```bash
   docker run --rm -v postpilot_certs:/etc/letsencrypt -v postpilot_acme:/var/www/certbot \
     certbot/certbot certonly --webroot -w /var/www/certbot -d yourdomain.com -d www.yourdomain.com \
     --email you@example.com --agree-tos
   ```
   Restore the `443` block and run `docker compose restart nginx`.
5. **Admin user and plans**
   ```bash
   docker compose exec web python manage.py createsuperuser
   ```
   Open `/admin/`, set each paid Plan's `razorpay_plan_id`.
6. **Backups** (nightly cron):
   ```bash
   0 2 * * * cd /opt/postpilot && docker compose exec -T db pg_dump -U postpilot postpilot | gzip > /var/backups/postpilot-$(date +\%F).sql.gz
   ```
7. **Updates**: `git pull && docker compose up -d --build` (migrations run on web start).
8. **Scaling later**: move `worker-generate` and `worker-publish` to a second VPS pointing at the same
   `REDIS_URL` and Postgres; add `--scale worker-generate=3`.

Logs: `docker compose logs -f worker-generate worker-publish beat`. Add Sentry by installing
`sentry-sdk` and calling `sentry_sdk.init(dsn=...)` in `config/settings.py` when `SENTRY_DSN` is set.

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
- **Razorpay** (INR): create three monthly plans, paste ids into `razorpay_plan_id`, add the webhook.
- Prices in `seed_plans` (USD 9 / 29 / 99, INR 599 / 1999 / 7499) are proposals. Edit them in `/admin/`
  and make sure the Stripe/Razorpay prices match.
