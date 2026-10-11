from django.contrib import admin

from .models import Plan, Subscription, UsageEvent, WalletEntry

admin.site.register(Plan)
admin.site.register(Subscription)
admin.site.register(UsageEvent)


@admin.register(WalletEntry)
class WalletEntryAdmin(admin.ModelAdmin):
    list_display = ("workspace", "kind", "status", "currency", "amount_minor", "created_at")
    list_filter = ("kind", "status", "currency")
    search_fields = ("workspace__name", "reason", "provider_checkout_id", "provider_subscription_id")
    readonly_fields = tuple(field.name for field in WalletEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
