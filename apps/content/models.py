from django.db import models
from django.utils import timezone

from apps.social.models import SocialAccount
from apps.workspaces.models import Workspace


class Category(models.Model):
    """A content 'recipe' stored as data (PRD section 5): source, freshness, prompt, risk."""

    SOURCE_TYPES = [
        ("rss", "RSS feeds"),
        ("newsdata", "newsdata.io API"),
        ("hn", "Hacker News top stories"),
        ("apple_podcasts", "Apple Podcasts top charts"),
        ("evergreen", "Evergreen topics (no fetch)"),
        ("followed", "A podcast, channel or site you follow"),
    ]
    RISK = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    MODES = [("auto", "Auto-publish"), ("approve", "Approve first"), ("draft", "Draft only")]

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    icon = models.CharField(max_length=8, blank=True)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES)
    source_config = models.JSONField(default=dict, blank=True)
    system_prompt = models.TextField(help_text="Category specific writing rules, including do and don't")
    prompt_version = models.CharField(max_length=20, default="v1")
    risk_level = models.CharField(max_length=10, choices=RISK, default="medium")
    default_mode = models.CharField(max_length=10, choices=MODES, default="approve")
    disclaimer = models.CharField(max_length=300, blank=True, help_text="Appended to every post of this category")
    freshness_hours = models.PositiveIntegerField(default=24)
    needs_source = models.BooleanField(default=False, help_text="Posts must be grounded in a fetched source")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def forces_approval(self):
        return self.risk_level == "high"


REGIONS = [
    ("US", "United States"), ("GB", "United Kingdom"), ("IN", "India"), ("CA", "Canada"), ("AU", "Australia"),
    ("NZ", "New Zealand"), ("IE", "Ireland"), ("SG", "Singapore"), ("AE", "United Arab Emirates"),
    ("ZA", "South Africa"), ("NG", "Nigeria"), ("KE", "Kenya"), ("PH", "Philippines"), ("PK", "Pakistan"),
    ("MY", "Malaysia"),
]


class FollowedSource(models.Model):
    """A podcast, YouTube channel or site feed a workspace follows. New episodes become post ideas."""

    KINDS = [("podcast", "Podcast or interview show"), ("youtube", "YouTube channel"), ("website", "Blog or news site")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="followed_sources")
    kind = models.CharField(max_length=10, choices=KINDS)
    name = models.CharField(max_length=160)
    feed_url = models.URLField(max_length=600)
    site_url = models.URLField(max_length=600, blank=True)
    enabled = models.BooleanField(default=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "feed_url")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class TrendItem(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="trend_items")
    source = models.ForeignKey(FollowedSource, null=True, blank=True, on_delete=models.CASCADE, related_name="items")
    region = models.CharField(max_length=2, default="US", db_index=True)
    url = models.URLField(max_length=600)
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True)
    score = models.FloatField(default=0)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["category", "region", "url"], condition=models.Q(source__isnull=True), name="uniq_trend_shared"),
            models.UniqueConstraint(fields=["source", "url"], condition=models.Q(source__isnull=False), name="uniq_trend_followed"),
        ]
        ordering = ["-score", "-fetched_at"]

    def __str__(self):
        return self.title


FORMATS = [
    ("auto", "Automatic (best for the platform)"),
    ("text", "Text only"),
    ("image", "Text + image card"),
    ("video", "Short vertical video"),
]
LANGUAGES = [
    ("", "Same as brand kit"), ("English", "English"), ("Hindi", "Hindi (Devanagari)"), ("Hinglish", "Hinglish (Hindi in Latin letters)"),
    ("Spanish", "Spanish"), ("Portuguese", "Portuguese"), ("French", "French"), ("German", "German"), ("Arabic", "Arabic"),
    ("Indonesian", "Indonesian"), ("Bengali", "Bengali"), ("Tamil", "Tamil"), ("Telugu", "Telugu"), ("Marathi", "Marathi"),
    ("Gujarati", "Gujarati"), ("Urdu", "Urdu"),
]
STYLES = [
    ("standard", "Standard post"),
    ("explainer", "Simple explainer"),
    ("listicle", "Numbered list"),
    ("story", "Mini story"),
    ("quiz", "Quiz or poll question"),
    ("quote", "Punchy one-liner"),
]


class AccountCategory(models.Model):
    """Links a connected account to a category, with its own schedule and publish mode."""

    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="subscriptions")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="subscriptions")
    post_times = models.JSONField(default=list, help_text='Local times, e.g. ["09:00", "18:30"]')
    publish_mode = models.CharField(max_length=10, choices=Category.MODES, default="approve")
    custom_topic = models.CharField(max_length=200, blank=True, help_text="Optional focus inside the category")
    region = models.CharField(max_length=2, choices=REGIONS, default="US", help_text="Which country's news and charts to use")
    followed_source = models.ForeignKey(FollowedSource, null=True, blank=True, on_delete=models.CASCADE, related_name="subscriptions")
    content_format = models.CharField(max_length=10, choices=FORMATS, default="auto", help_text="What each post contains")
    language = models.CharField(max_length=20, choices=LANGUAGES, blank=True, default="", help_text="Overrides the brand kit language for this category")
    post_style = models.CharField(max_length=12, choices=STYLES, default="standard", help_text="How the post is written")
    auto_times = models.BooleanField(default=False, help_text="Learn the best hours from how your posts perform")
    auto_ack = models.BooleanField(default=False, help_text="Owner accepted auto-publishing a high risk category")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "category"], condition=models.Q(followed_source__isnull=True), name="uniq_sub_category"),
            models.UniqueConstraint(fields=["account", "followed_source"], condition=models.Q(followed_source__isnull=False), name="uniq_sub_source"),
        ]

    def __str__(self):
        return f"{self.account} / {self.label}"

    @property
    def label(self):
        return f"{self.category.name}: {self.followed_source.name}" if self.followed_source_id else self.category.name

    @property
    def effective_mode(self):
        """High risk categories auto-publish only after the owner explicitly accepted the risk."""
        if self.category.forces_approval and self.publish_mode == "auto" and not self.auto_ack:
            return "approve"
        return self.publish_mode


class Post(models.Model):
    STATUSES = [
        ("generating", "Generating"),
        ("draft", "Draft"),
        ("pending_approval", "Waiting for approval"),
        ("needs_review", "Needs review"),
        ("queued", "Queued"),
        ("publishing", "Publishing"),
        ("published", "Published"),
        ("failed", "Failed"),
        ("rejected", "Rejected"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="posts")
    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="posts")
    subscription = models.ForeignKey(AccountCategory, on_delete=models.SET_NULL, null=True, related_name="posts")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="posts")
    idea = models.CharField(max_length=300, blank=True, help_text="Topic typed by the user for a one-off post")
    slot_key = models.CharField(max_length=60, help_text="Unique per subscription and time slot: stops double generation")
    status = models.CharField(max_length=20, choices=STATUSES, default="generating", db_index=True)

    caption = models.TextField(blank=True)
    hashtags = models.JSONField(default=list, blank=True)
    image_text = models.CharField(max_length=300, blank=True)
    alt_text = models.CharField(max_length=300, blank=True)
    media_path = models.CharField(max_length=300, blank=True, help_text="Image card, relative to MEDIA_ROOT")
    video_path = models.CharField(max_length=300, blank=True, help_text="Vertical clip for YouTube Shorts, relative to MEDIA_ROOT")
    source_title = models.CharField(max_length=300, blank=True)
    source_url = models.URLField(max_length=600, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    review_notes = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    dry_run = models.BooleanField(default=False, help_text="Generated without calling Claude. Never auto-published.")

    scheduled_at = models.DateTimeField(db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    platform_post_id = models.CharField(max_length=120, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_at"]
        constraints = [
            models.UniqueConstraint(fields=["subscription", "slot_key"], name="uniq_post_per_slot"),
        ]

    def __str__(self):
        return f"Post {self.pk} ({self.status})"

    def full_text(self):
        """Final text sent to the platform: caption + hashtags + CTA + disclaimer."""
        parts = [self.caption.strip()]
        if self.hashtags:
            parts.append(" ".join(self.hashtags))
        brand = getattr(self.workspace, "brand", None)
        if brand and brand.cta:
            parts.append(brand.cta)
        if self.category.disclaimer:
            parts.append(self.category.disclaimer)
        if self.source_url and self.category.needs_source:
            parts.append(f"Source: {self.source_url}")
        return "\n\n".join(p for p in parts if p)

    @property
    def media_url(self):
        from django.conf import settings

        return f"{settings.MEDIA_URL}{self.media_path}" if self.media_path else ""

    @property
    def video_url(self):
        from django.conf import settings

        return f"{settings.MEDIA_URL}{self.video_path}" if self.video_path else ""

    @property
    def can_approve(self):
        return self.status in ("pending_approval", "needs_review", "draft") and bool(self.caption) and not self.dry_run


class GenerationRun(models.Model):
    """Audit trail and cost tracking: one row per Claude call (PRD section 7)."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="runs")
    stage = models.CharField(max_length=20)
    model = models.CharField(max_length=60)
    prompt_version = models.CharField(max_length=20, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    verdict = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class PostMetrics(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="metrics")
    reach = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)
    comments = models.PositiveIntegerField(default=0)
    shares = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(default=timezone.now)
