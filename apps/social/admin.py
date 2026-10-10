from django.contrib import admin

from .models import SocialAccount


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "platform", "workspace", "status", "last_checked_at")
    list_filter = ("platform", "status")
    exclude = ("token_enc",)
