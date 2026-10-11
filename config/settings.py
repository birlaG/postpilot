"""PostPilot settings. Everything secret or environment-specific comes from env vars."""
import hashlib
import base64
import os
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def load_local_env():
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env()


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(os.environ.get(key, str(default))).strip().lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


# ---------------------------------------------------------------- core
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

if not DEBUG:
    if SECRET_KEY == "dev-insecure-change-me-in-production":
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY before running with DJANGO_DEBUG=0.")
    if not env("POSTGRES_DB"):
        raise ImproperlyConfigured("Set POSTGRES_DB before running with DJANGO_DEBUG=0.")
    if not env("FERNET_KEY"):
        raise ImproperlyConfigured("Set FERNET_KEY before running with DJANGO_DEBUG=0.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.workspaces",
    "apps.social",
    "apps.content",
    "apps.billing",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.web.middleware.WorkspaceMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER", "postpilot"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------- secrets at rest
# Fernet key for encrypting social OAuth/bot tokens. Set FERNET_KEY in production
# (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
FERNET_KEY = env("FERNET_KEY") or base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()).decode()

# ---------------------------------------------------------------- Claude / AI engine
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
# Dry-run is on automatically when no API key is set. Dry-run posts are clearly fake
# and are never auto-published.
AI_DRY_RUN = env_bool("AI_DRY_RUN", not bool(ANTHROPIC_API_KEY))
# One model per pipeline stage; swap via env without a deploy (see PRD section 7).
AI_MODEL_TRIAGE = env("AI_MODEL_TRIAGE", "claude-haiku-5-5")
AI_MODEL_WRITE = env("AI_MODEL_WRITE", "claude-sonnet-5-5")
AI_MODEL_REVIEW = env("AI_MODEL_REVIEW", "claude-haiku-5-5")
AI_MODEL_REVIEW_HIGH = env("AI_MODEL_REVIEW_HIGH", "claude-opus-5-5")
AI_MAX_ATTEMPTS = int(env("AI_MAX_ATTEMPTS", "3"))
# USD per million tokens (input, output). PLACEHOLDERS: copy the real numbers from the
# Anthropic pricing page. Cost tracking is only as accurate as these values.
AI_PRICES = {
    AI_MODEL_TRIAGE: (float(env("PRICE_TRIAGE_IN", "1")), float(env("PRICE_TRIAGE_OUT", "5"))),
    AI_MODEL_WRITE: (float(env("PRICE_WRITE_IN", "3")), float(env("PRICE_WRITE_OUT", "15"))),
    AI_MODEL_REVIEW: (float(env("PRICE_REVIEW_IN", "1")), float(env("PRICE_REVIEW_OUT", "5"))),
    AI_MODEL_REVIEW_HIGH: (float(env("PRICE_REVIEW_HIGH_IN", "5")), float(env("PRICE_REVIEW_HIGH_OUT", "25"))),
}

# ---------------------------------------------------------------- trend sources
NEWSDATA_API_KEY = env("NEWSDATA_API_KEY", "")
HTTP_TIMEOUT = int(env("HTTP_TIMEOUT", "20"))
GRAPH_API_VERSION = env("GRAPH_API_VERSION", "v21.0")  # check Meta docs for the current version

# ---------------------------------------------------------------- billing (Razorpay)
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET", "")
BILLING_GRACE_DAYS = int(env("BILLING_GRACE_DAYS", "3"))
# Stripe is used for USD (worldwide). Razorpay is used for INR (India). Set one or both.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "")

# ---------------------------------------------------------------- platform OAuth apps
# Create one developer app per platform and set the redirect URI to
# {PUBLIC_BASE_URL}/oauth/<provider>/callback/   (provider = google, meta, x, linkedin)
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", "")
META_APP_ID = env("META_APP_ID", "")
META_APP_SECRET = env("META_APP_SECRET", "")
X_CLIENT_ID = env("X_CLIENT_ID", "")
X_CLIENT_SECRET = env("X_CLIENT_SECRET", "")
LINKEDIN_CLIENT_ID = env("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = env("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_API_VERSION = env("LINKEDIN_API_VERSION", "202510")  # YYYYMM, check LinkedIn docs for a supported one
VIDEO_SECONDS = int(env("VIDEO_SECONDS", "8"))  # length of the generated YouTube Shorts clip

# ---------------------------------------------------------------- Celery
CELERY_BROKER_URL = env("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = None
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_EAGER", False)
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = "UTC"
CELERY_TASK_DEFAULT_QUEUE = "generate"
CELERY_TASK_ROUTES = {
    "apps.content.tasks.ingest_trends": {"queue": "ingest"},
    "apps.content.tasks.plan_slots": {"queue": "generate"},
    "apps.content.tasks.generate_post_task": {"queue": "generate"},
    "apps.content.tasks.publish_due_posts": {"queue": "publish"},
    "apps.content.tasks.publish_post_task": {"queue": "publish"},
    "apps.content.tasks.check_accounts": {"queue": "publish"},
}
CELERY_BEAT_SCHEDULE = {
    "ingest-trends-hourly": {"task": "apps.content.tasks.ingest_trends", "schedule": crontab(minute=5)},
    "plan-slots": {"task": "apps.content.tasks.plan_slots", "schedule": 300.0},
    "publish-due": {"task": "apps.content.tasks.publish_due_posts", "schedule": 60.0},
    "collect-metrics": {"task": "apps.content.tasks.collect_metrics", "schedule": crontab(minute=20, hour="*/3")},
    "check-accounts-daily": {"task": "apps.content.tasks.check_accounts", "schedule": crontab(hour=3, minute=0)},
}

# ---------------------------------------------------------------- pipeline knobs
GENERATE_LEAD_MINUTES = int(env("GENERATE_LEAD_MINUTES", "30"))   # generate this long before the slot
SLOT_CATCHUP_MINUTES = int(env("SLOT_CATCHUP_MINUTES", "180"))    # still generate if the worker was down
PUBLISH_MAX_ATTEMPTS = int(env("PUBLISH_MAX_ATTEMPTS", "3"))
REFERRAL_BONUS_POSTS = int(env("REFERRAL_BONUS_POSTS", "50"))    # extra posts per month, for both sides
REFERRAL_BONUS_DAYS = int(env("REFERRAL_BONUS_DAYS", "90"))
REFERRAL_MAX_REWARDS = int(env("REFERRAL_MAX_REWARDS", "10"))    # rewarded invites per workspace
MAX_FOLLOWED_SOURCES = int(env("MAX_FOLLOWED_SOURCES", "10"))
TREND_RETENTION_DAYS = int(env("TREND_RETENTION_DAYS", "7"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
