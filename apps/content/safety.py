"""Cheap, deterministic checks that run before and after the Claude reviewer."""
import hashlib
import re

# Deliberately small. The Claude review stage does the nuanced work; this is the seatbelt.
GLOBAL_BLOCKLIST = [
    "guaranteed returns", "guaranteed profit", "sure shot", "miracle cure", "cures cancer",
    "buy now only", "click here to win",
]


def content_hash(text: str) -> str:
    normal = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha256(normal.encode()).hexdigest()


def static_checks(*, caption, hashtags, platform_limits, brand, full_text_len):
    """Return a list of human readable problems. Empty list = OK."""
    problems = []
    if not caption or len(caption.strip()) < 10:
        problems.append("Caption is empty or too short.")
    lowered = f" {caption.lower()} "
    for word in brand.banned_list():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            problems.append(f"Contains banned word: {word}")
    for phrase in GLOBAL_BLOCKLIST:
        if phrase in lowered:
            problems.append(f"Contains risky phrase: {phrase}")
    if full_text_len > platform_limits["max_chars"]:
        problems.append(
            f"Final text is {full_text_len} characters, limit is {platform_limits['max_chars']}. Make the caption shorter."
        )
    if len(hashtags) > platform_limits["max_hashtags"]:
        problems.append(f"Use at most {platform_limits['max_hashtags']} hashtags.")
    return problems


def clean_hashtags(tags, limit):
    out = []
    for tag in tags or []:
        tag = re.sub(r"[^\w#]", "", str(tag))
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        if tag.lower() not in [t.lower() for t in out]:
            out.append(tag)
    return out[:limit]
