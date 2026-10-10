from django.db import models

from apps.workspaces.models import Workspace

from . import crypto


class SocialAccount(models.Model):
    PLATFORMS = [
        ("telegram", "Telegram channel"),
        ("facebook", "Facebook Page"),
        ("instagram", "Instagram"),
        ("youtube", "YouTube"),
        ("x", "X (Twitter)"),
        ("linkedin", "LinkedIn"),
    ]
    STATUSES = [
        ("active", "Active"),
        ("expiring", "Expiring soon"),
        ("broken", "Needs reconnect"),
        ("disabled", "Disabled"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="accounts")
    platform = models.CharField(max_length=20, choices=PLATFORMS)
    display_name = models.CharField(max_length=120)
    handle = models.CharField(max_length=120, blank=True)
    token_enc = models.TextField(blank=True, editable=False)
    # platform specific ids, e.g. {"chat_id": "@mychannel"} or {"page_id": "123"}
    extra = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="active")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["platform", "display_name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "platform"], name="uniq_social_account_per_platform"),
        ]

    def __str__(self):
        return f"{self.get_platform_display()}: {self.display_name}"

    @property
    def token(self) -> str:
        return crypto.decrypt(self.token_enc)

    @token.setter
    def token(self, value: str):
        self.token_enc = crypto.encrypt(value)

    @property
    def creds(self) -> dict:
        """OAuth credentials: {access_token, refresh_token, expires_at(epoch)} stored encrypted as JSON."""
        import json

        try:
            data = json.loads(self.token)
        except ValueError:
            return {"access_token": self.token}
        return data if isinstance(data, dict) else {"access_token": self.token}

    def set_creds(self, access_token, refresh_token="", expires_at=None):
        import json

        self.token = json.dumps({"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at})

    @property
    def is_usable(self):
        return self.status in ("active", "expiring")
