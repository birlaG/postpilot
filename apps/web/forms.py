import re
import zoneinfo

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from apps.content.models import FORMATS, REGIONS, AccountCategory, FollowedSource
from apps.content.scheduling import parse_times
from apps.social.limits import allowed_formats
from apps.workspaces.models import BrandKit

User = get_user_model()
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _valid_timezone(value):
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise forms.ValidationError("Use a timezone name like America/New_York or Asia/Kolkata.")
    return value


class SignupForm(forms.Form):
    name = forms.CharField(max_length=120, label="Your name or brand")
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)
    country = forms.ChoiceField(choices=REGIONS, initial="US", label="Main audience country",
                                help_text="Used for local news and podcast charts. You can change it per category later.")
    timezone = forms.CharField(initial="UTC", max_length=64, label="Your timezone", validators=[_valid_timezone],
                               help_text="e.g. America/New_York, Europe/London, Asia/Kolkata. Posting times use this.")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(username=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password)
        return password


class BrandKitForm(forms.ModelForm):
    country = forms.ChoiceField(choices=REGIONS, label="Main audience country")
    timezone = forms.CharField(max_length=64, label="Timezone", validators=[_valid_timezone])

    class Meta:
        model = BrandKit
        exclude = ["workspace"]
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "secondary_color": forms.TextInput(attrs={"type": "color"}),
            "hashtags": forms.Textarea(attrs={"rows": 2}),
            "banned_words": forms.Textarea(attrs={"rows": 2}),
            "sample_posts": forms.Textarea(attrs={"rows": 6}),
        }

    def clean_primary_color(self):
        v = self.cleaned_data["primary_color"]
        if not HEX.match(v):
            raise forms.ValidationError("Use a #RRGGBB colour.")
        return v

    clean_secondary_color = clean_primary_color


class TelegramConnectForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Channel name")
    bot_token = forms.CharField(widget=forms.PasswordInput(render_value=False), label="Bot token (from @BotFather)")
    chat_id = forms.CharField(max_length=80, label="Channel username or id", help_text="e.g. @mychannel. Add your bot as an admin first.")


class FacebookConnectForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Page name")
    page_id = forms.CharField(max_length=40)
    page_token = forms.CharField(widget=forms.PasswordInput(render_value=False), label="Page access token")


class InstagramConnectForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Account name")
    handle = forms.CharField(max_length=60, required=False)
    ig_user_id = forms.CharField(max_length=40, label="Instagram business account id")
    token = forms.CharField(widget=forms.PasswordInput(render_value=False), label="Access token")


CONNECT_FORMS = {"telegram": TelegramConnectForm, "facebook": FacebookConnectForm, "instagram": InstagramConnectForm}


class SubscriptionForm(forms.ModelForm):
    post_times_text = forms.CharField(
        label="Posting times (your local time)", help_text="Comma separated 24h times, e.g. 09:00, 18:30"
    )

    class Meta:
        model = AccountCategory
        fields = ["content_format", "post_style", "language", "publish_mode", "auto_ack", "auto_times", "region", "custom_topic", "enabled"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            allowed = allowed_formats(self.instance.account.platform)
            self.fields["content_format"].choices = [c for c in FORMATS if c[0] == "auto" or c[0] in allowed]
            self.fields["post_times_text"].initial = ", ".join(self.instance.post_times)
        else:
            self.fields["post_times_text"].initial = "09:00"

    def clean(self):
        data = super().clean()
        if data.get("publish_mode") != "auto":
            data["auto_ack"] = False
        elif self.instance.category.forces_approval and not data.get("auto_ack"):
            self.add_error("auto_ack", "Tick this box to auto-publish a high risk category.")
        return data

    def clean_post_times_text(self):
        raw = [p.strip() for p in self.cleaned_data["post_times_text"].split(",") if p.strip()]
        parsed = parse_times(raw)
        if not parsed or len(parsed) != len(raw):
            raise forms.ValidationError("Enter valid times like 09:00, 18:30.")
        if len(parsed) > 5:
            raise forms.ValidationError("At most 5 posts per day per category.")
        return [f"{t:%H:%M}" for t in parsed]

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.post_times = self.cleaned_data["post_times_text"]
        if commit:
            obj.save()
        return obj


class FollowSourceForm(forms.Form):
    url = forms.URLField(max_length=500, label="Link",
                         help_text="A podcast RSS or Apple Podcasts link, a YouTube channel link, or a blog or news site.")
    kind = forms.ChoiceField(choices=[("", "Detect automatically")] + FollowedSource.KINDS, required=False, label="Type")


class IdeaForm(forms.Form):
    idea = forms.CharField(max_length=300, min_length=5, label="Write about")


class PostEditForm(forms.Form):
    caption = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}))
    hashtags = forms.CharField(required=False, help_text="Space separated")
