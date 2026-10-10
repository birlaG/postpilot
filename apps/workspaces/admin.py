from django.contrib import admin

from .models import BrandKit, Member, Workspace

admin.site.register(Workspace)
admin.site.register(Member)
admin.site.register(BrandKit)
