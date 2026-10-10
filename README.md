# PostPilot

AI social media content automation, worldwide. Users sign up, pay for a plan (there is no free tier),
connect accounts, pick categories (trending news, podcasts, daily jokes, new technology, spiritual,
motivation, finance, health, sports, facts), and the platform researches, writes with Claude, renders an
image card or Shorts clip, and publishes on a schedule, with optional approval.

Stack: Django, Celery, Redis, PostgreSQL, Nginx, Stripe (USD) and Razorpay (INR).

## What works

| Area | Status |
| --- | --- |
| Signup with country and timezone, workspaces, roles (owner/editor/viewer) | Done |
| **Paid only**: no trial. New workspaces are locked until a plan is paid | Done |
| Stripe Checkout (USD) and Razorpay subscriptions (INR), signature-verified webhooks, 3-day grace period | Done |
| Platforms: **YouTube Shorts, Instagram, Facebook Pages, X, LinkedIn**, plus Telegram | Done, mock-tested |
| OAuth sign-in connect for YouTube, Facebook + Instagram, X, LinkedIn; token refresh and rotation | Done, mock-tested |
| Paste-a-token connect for Telegram, and a fallback for Facebook/Instagram | Done |
| **Content type per category**: text only, image card or short video, and a writing style (explainer, list, mini story, quiz, one-liner) | Done |
| **Follow a podcast, interview show, YouTube channel or site**: new episodes become credited, linked posts | Done, mock-tested |
| Public landing page with pricing, onboarding checklist, workspace-local content calendar and post history, streak and time-saved stats | Done |
| **Analytics**: views, likes, comments, shares per post, by topic and platform, best posting hours (YouTube, X, Facebook, Instagram; not LinkedIn or Telegram) | Done, mock-tested |
| **Invite and earn**: both sides get 50 extra posts a month for 90 days after the friend's first payment (capped, no self-invites) | Done |
| **Best-time learning** (opt-in per category): moves posts to the hours that earn the most engagement, keeps one slot a day for testing | Done |
| **Content generation**: language per category (15 languages), voice samples in the brand kit, "write about my idea", and "ask Claude to rewrite" with the same safety checks and review | Done |
| **Mobile**: bottom tab bar, touch-sized controls, no sideways scrolling on any page, installable to the home screen (PWA manifest and icons) | Done |
| Brand card themes (gradient, solid, dark, light, bold) with live preview | Done |
| 17 category recipes as data, per-account schedule, publish mode, **region per category** | Done |
| Trend ingestion per region: Google News RSS, newsdata.io, Hacker News, Apple Podcasts charts | Done |
| Claude pipeline: triage, write, static checks, independent review (Opus for high risk), retries, cost log | Done |
| Image cards (1080x1080) and vertical Shorts clips (1080x1920, ffmpeg), brand kit | Done |
| Approval queue, idempotent publisher, retries, account health checks | Done |
| Docker Compose, Nginx, Postgres, separate generate/publish workers | Done |
| 124 automated tests | Passing |
| Google sign-in and mobile OTP login, metrics fetching, WhatsApp approvals, TikTok/Pinterest/Threads | Not built |

**Everything touching an outside service is tested with mocks only.** Before launch, run each platform,
Stripe/Razorpay and the Claude calls once with real test accounts. The endpoints, scopes and API versions
(`GRAPH_API_VERSION`, `LINKEDIN_API_VERSION`, X media upload, YouTube quota) come from my knowledge of
these APIs and may have changed; confirm each against the current docs.

## Plans (proposals, edit in /admin/)

| Plan | USD / month | INR / month | Accounts | Posts / month | YouTube Shorts |
| --- | --- | --- | --- | --- | --- |
| Starter | 9 | 599 | 3 | 90 | No |
| Pro | 29 | 1,999 | 6 | 300 | Yes |
| Agency | 99 | 7,499 | 25 | 1,500 | Yes |

The dashboard shows each workspace's monthly AI cost so you can check margins; set the real token
prices in `.env` first (the defaults are placeholders).

## Run locally (no Redis, no Docker)

Settings load values from `.env` automatically. If it contains the Docker Compose `POSTGRES_DB`
setting, clear it for a local SQLite run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # also install ffmpeg for YouTube Shorts
# Local-only overrides: debug static files, inline tasks, SQLite, and local/Codespaces hosts.
export DJANGO_DEBUG=1 CELERY_EAGER=1 POSTGRES_DB= DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.app.github.dev
python manage.py migrate
python manage.py seed_plans && python manage.py seed_categories
python manage.py createsuperuser
python manage.py runserver
```

Sign up, then on the Plan page use the `[dev] activate` button (only works with `DJANGO_DEBUG=1`).
Connect a Telegram channel (bot from @BotFather, added as channel admin), add a category, press
**Generate now**.

Without `ANTHROPIC_API_KEY` the app runs in **dry-run mode**: placeholder posts that cannot be approved or
published. Set the key to generate real posts: `python manage.py generate_now 1 --ingest`.

Tests: `python manage.py test tests`

## How it fits together

```
config/            settings, celery app, urls
apps/workspaces/   Workspace (country, timezone), Member, BrandKit
apps/social/       SocialAccount (encrypted credentials), oauth.py, publishers/
                   telegram, meta (facebook, instagram), youtube, xtwitter, linkedin
apps/content/      Category recipes, TrendItem (per region), Post, GenerationRun
    ai.py          Claude calls per stage, prompt caching, cost tracking, dry-run
    trends.py      fetchers + region placeholders     pipeline.py  triage > write > check > review > media
    scheduling.py  slot claiming                       publishing.py claim, publish, retry, health check
    media.py       image cards and ffmpeg clips        safety.py     static checks
apps/billing/      Plan, Subscription, limits, Stripe + Razorpay
apps/web/          dashboard, approval queue, accounts, brand kit, billing pages
```

**Daily loop** (Celery beat): `ingest_trends` hourly (only regions someone subscribed to), `plan_slots`
every 5 minutes (claims a Post row per due slot, so a slot never generates twice), `publish_due_posts`
every minute (atomic `queued -> publishing`, so a post never sends twice), `check_accounts` nightly.

**Followed sources**: paste a podcast RSS or Apple Podcasts link, a YouTube channel link, or a site; we find the
feed, read the newest items hourly and write from the title and description only (no audio, video or transcripts).
Posts always credit the show and link the original, and go to approval by default. User supplied URLs are fetched
through `apps/content/netsafe.py` (public addresses only, redirects re-checked, size capped, XML parsed with
defusedxml). Also block private ranges in the server firewall.

**Safety defaults**: high-risk categories (news, finance, health, spiritual) stay on approval unless the owner ticks "I accept the risk" to auto-publish; a post that fails the AI review or safety checks never auto-publishes; sourced
categories must cite a fetched source; the reviewer must pass or the post goes to "needs review";
dry-run posts never publish; viewers cannot change anything; tokens are encrypted and deleted on disconnect.

## Deploy

See `deploy/README-deploy.md` (server, HTTPS, backups, **platform developer apps and payments setup**) and
`.env.example`.

## Launch checklist

- [ ] Create the five platform developer apps and start Meta review and the YouTube audit today.
- [ ] Create Stripe prices (and Razorpay plans if selling in India); paste ids into the Plans in `/admin/`.
- [ ] Set real Claude model names and token prices in `.env`.
- [ ] Send one real post to every platform from a test account, including a YouTube Short.
- [ ] X: confirm your API tier's monthly post cap fits your plans before enabling X on Starter.
- [ ] Add a privacy policy, terms and a refund policy (GDPR, UK GDPR, India DPDP): consent, export, deletion.
- [ ] Add Sentry and an uptime check; test restoring the nightly backup.
- [ ] Decide on VAT/GST handling: Stripe Tax can calculate it for global customers.

## Next build steps

1. Google sign-in and mobile OTP; multi-language dashboard.
2. LinkedIn metrics (needs extra API access).
3. TikTok, Pinterest, Threads adapters; Reels for Instagram.
4. Voice-over and captions for Shorts; WhatsApp/Telegram one-tap approvals; agency mode.
