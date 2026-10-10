"""Per-platform output rules used by the writer prompt and by static checks.

Limits are deliberately a little under the hard platform maximums.
Verify against each platform's current docs before relying on them.
"""

PLATFORM_LIMITS = {
    "telegram": {"max_chars": 1000, "max_hashtags": 3, "style": "short, punchy, one idea, emoji allowed sparingly"},
    "facebook": {"max_chars": 1500, "max_hashtags": 3, "style": "conversational, ask a question at the end"},
    "instagram": {"max_chars": 2000, "max_hashtags": 10, "style": "strong first line hook, line breaks, hashtags at the end"},
    "youtube": {"max_chars": 4500, "max_hashtags": 3, "style": "YouTube Shorts: the FIRST LINE is the video title (max 90 characters, no hashtags), then a short description"},
    "linkedin": {"max_chars": 2800, "max_hashtags": 4, "style": "professional, short paragraphs, a strong first line, end with a question"},
    "x": {"max_chars": 270, "max_hashtags": 2, "style": "one sharp sentence or two, no filler"},
}


def limits_for(platform: str) -> dict:
    return PLATFORM_LIMITS.get(platform, PLATFORM_LIMITS["facebook"])


# Which content formats each platform can publish. Instagram cannot post text alone and
# YouTube needs a video, so those are fixed.
ALLOWED_FORMATS = {
    "telegram": ["text", "image"],
    "facebook": ["text", "image"],
    "instagram": ["image"],
    "youtube": ["video"],
    "x": ["text", "image"],
    "linkedin": ["text", "image"],
}


def allowed_formats(platform: str) -> list:
    return ALLOWED_FORMATS.get(platform, ["text", "image"])


def resolve_format(platform: str, chosen: str) -> str:
    """'auto' keeps the original behaviour: video for YouTube, an image card everywhere else."""
    allowed = allowed_formats(platform)
    if chosen in allowed:
        return chosen
    return "video" if platform == "youtube" else "image"
