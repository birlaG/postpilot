from django.contrib import admin

from .models import AccountCategory, Category, FollowedSource, GenerationRun, Post, PostMetrics, TrendItem


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "source_type", "risk_level", "default_mode", "is_active")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "category", "status", "scheduled_at", "dry_run")
    list_filter = ("status", "category", "dry_run")


@admin.register(GenerationRun)
class GenerationRunAdmin(admin.ModelAdmin):
    list_display = ("post", "stage", "model", "tokens_in", "tokens_out", "cost_usd", "created_at")


admin.site.register(TrendItem)
admin.site.register(AccountCategory)
admin.site.register(PostMetrics)
admin.site.register(FollowedSource)
