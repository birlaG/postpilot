import zoneinfo

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    """Billing and isolation unit. Every tenant-owned row points at one."""

    name = models.CharField(max_length=120)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_workspaces")
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA name, e.g. America/New_York")
    country = models.CharField(max_length=2, default="US", help_text="Default region for news and charts")
    referral_code = models.CharField(max_length=12, unique=True, null=True, blank=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.referral_code:
            import secrets

            alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters
            while True:
                code = "".join(secrets.choice(alphabet) for _ in range(8))
                if not Workspace.objects.filter(referral_code=code).exists():
                    self.referral_code = code
                    break
        super().save(*args, **kwargs)

    @property
    def tz(self):
        try:
            return zoneinfo.ZoneInfo(self.timezone)
        except zoneinfo.ZoneInfoNotFoundError:
            return zoneinfo.ZoneInfo("UTC")


class Member(models.Model):
    ROLES = [("owner", "Owner"), ("editor", "Editor"), ("viewer", "Viewer")]
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=ROLES, default="editor")

    class Meta:
        unique_together = [("workspace", "user")]

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role})"

    @property
    def can_edit(self):
        return self.role in ("owner", "editor")


def _split(value):
    return [p.strip() for p in (value or "").replace("\n", ",").split(",") if p.strip()]


class BrandKit(models.Model):
    """Injected into every prompt and used when rendering image cards."""

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="brand")
    tone = models.CharField(max_length=60, default="friendly", help_text="e.g. funny, formal, devotional, energetic")
    language = models.CharField(max_length=40, default="English", help_text="e.g. English, Hindi, Hinglish")
    primary_color = models.CharField(max_length=7, default="#4F46E5")
    secondary_color = models.CharField(max_length=7, default="#111827")
    hashtags = models.TextField(blank=True, help_text="Comma separated, always considered")
    banned_words = models.TextField(blank=True, help_text="Comma separated; posts containing these are rejected")
    cta = models.CharField(max_length=200, blank=True, help_text="Optional call to action appended to posts")
    sample_posts = models.TextField(blank=True, help_text="Paste 2 or 3 posts you wrote yourself, separated by a blank line. Claude copies your voice, not your words.")
    CARD_THEMES = [
        ("gradient", "Gradient"),
        ("solid", "Solid colour"),
        ("dark", "Dark with accent bar"),
        ("light", "Clean light"),
        ("bold", "Bold centred"),
    ]
    card_theme = models.CharField(max_length=10, choices=CARD_THEMES, default="gradient", help_text="Look of your image cards and Shorts")
    watermark = models.CharField(max_length=60, blank=True, help_text="Shown in the footer of image cards")

    def __str__(self):
        return f"Brand kit for {self.workspace}"

    def hashtag_list(self):
        return [h if h.startswith("#") else f"#{h}" for h in _split(self.hashtags)]

    def banned_list(self):
        return [w.lower() for w in _split(self.banned_words)]
