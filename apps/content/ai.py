"""Claude API wrapper: one function per pipeline stage (PRD section 7).

Stages and default models (all swappable through env, see settings.py):
  triage  -> Haiku   picks the best trend item
  write   -> Sonnet  writes the platform native post
  review  -> Haiku   independent fact/safety review (Opus for high risk categories)

The stable part of every prompt (rules, brand voice, category rules) goes into a cached
system block, because it repeats for every post of an account.
"""
import json
import logging
import re
from decimal import Decimal

from django.conf import settings

log = logging.getLogger(__name__)


class AIError(Exception):
    pass


BASE_RULES = """You write social media posts for a content automation platform.
Hard rules:
- Write original text in your own words. Never copy sentences from a source article, podcast
  transcript, song lyrics or poem. Summarize and add value; credit the source by name when given.
- Use ONLY facts present in the provided source. If a fact is not in the source, leave it out.
  Never invent quotes, statistics, names, dates or events.
- No medical, legal or financial advice. No political persuasion. No hate, harassment, adult
  content or religious comparisons or claims of miracles/cures.
- Match the requested platform style, language and tone. Respect the length limit strictly.
- Do not repeat the openings or structure of the recent posts you are shown.
- Output ONLY a single JSON object, no markdown fences, no commentary."""


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=90)


def cost_usd(model, tokens_in, tokens_out):
    price_in, price_out = settings.AI_PRICES.get(model, (0.0, 0.0))
    return Decimal(str((tokens_in * price_in + tokens_out * price_out) / 1_000_000)).quantize(Decimal("0.00001"))


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating stray fences or prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AIError("Model did not return JSON")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as exc:
        raise AIError(f"Invalid JSON from model: {exc}") from exc


def call_json(stage, model, stable_system, user, max_tokens=1200):
    """Return (data, usage). usage = {model, tokens_in, tokens_out, cost_usd}."""
    if settings.AI_DRY_RUN:
        return _dry_run(stage, user), {"model": "dry-run", "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal(0)}
    try:
        resp = _client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": BASE_RULES},
                {"type": "text", "text": stable_system, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # anthropic raises several subclasses; the pipeline treats all alike
        raise AIError(f"Claude API call failed ({stage}): {exc}") from exc
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    data = extract_json(text)
    tin = resp.usage.input_tokens + (getattr(resp.usage, "cache_creation_input_tokens", 0) or 0) + (
        getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    )
    tout = resp.usage.output_tokens
    return data, {"model": model, "tokens_in": tin, "tokens_out": tout, "cost_usd": cost_usd(model, tin, tout)}


# ---------------------------------------------------------------- stages
def triage(candidates, category, brand):
    """candidates: list of dicts with id, title, summary. Returns the chosen id."""
    if len(candidates) == 1:
        return candidates[0]["id"], None
    system = f"Category: {category.name}. Pick the single item that is most interesting, timely and safe to post for this audience."
    user = json.dumps(
        {"task": 'Return {"pick": <id>, "reason": "<short>"}', "items": candidates[:12]},
        ensure_ascii=False,
    )
    data, usage = call_json("triage", settings.AI_MODEL_TRIAGE, system, user, max_tokens=200)
    pick = data.get("pick")
    valid = {c["id"] for c in candidates}
    return (pick if pick in valid else candidates[0]["id"]), usage


STYLE_RULES = {
    "standard": "A clear, well-shaped post.",
    "explainer": "Explain it simply in 2 to 4 short steps or sentences, as if to a curious friend.",
    "listicle": "Write a short numbered list of 3 to 5 points with a one-line intro.",
    "story": "Tell it as a mini story with a beginning, a turn and a closing line.",
    "quiz": "Ask a question first, give the answer after a line break, so readers can guess before reading on.",
    "quote": "One punchy, memorable line (max 2 sentences), written in your own words. Do not attribute it to a real person.",
}
FORMAT_RULES = {
    "text": "The post is published as text only, so make the caption complete on its own. image_text may be empty.",
    "image": "The post is published with an image card showing image_text, so the caption should add to it, not repeat it.",
    "video": "The post is published as a short vertical video showing image_text, so image_text must be a strong hook line.",
}


def write_post(*, category, brand, platform, limits, item, recent, feedback, custom_topic, fmt="image", style="standard", language=""):
    system = (
        f"Category: {category.name} (prompt {category.prompt_version})\n{category.system_prompt}\n\n"
        f"Brand voice: tone={brand.tone}; write the post in {language or brand.language} (hashtags may stay in English).\n"
        + (f"Voice examples written by the owner. Match their rhythm, tone and vocabulary level. Never copy their sentences or facts:\n{_samples(brand)}\n" if _samples(brand) else "")+
        f"Always-consider hashtags: {', '.join(brand.hashtag_list()) or 'none'}.\n"
        f"Banned words (never use): {', '.join(brand.banned_list()) or 'none'}."
    )
    user = json.dumps(
        {
            "task": "Write one post.",
            "platform": platform,
            "platform_style": limits["style"],
            "max_characters_for_caption_and_hashtags_together": limits["max_chars"] - 150,
            "max_hashtags": limits["max_hashtags"],
            "focus": custom_topic or None,
            "writing_style": STYLE_RULES.get(style, STYLE_RULES["standard"]),
            "content_format": FORMAT_RULES.get(fmt, FORMAT_RULES["image"]),
            "source": item,
            "source_note": ("The owner typed this topic themselves and no article exists. Write only general, well-established points, with no statistics, quotes, dates or news claims." if item.get("from_user") else None),
            "recent_posts_to_avoid_repeating": recent,
            "fix_these_problems_from_last_attempt": feedback or None,
            "output_schema": {
                "caption": "string",
                "hashtags": ["#tag"],
                "image_text": "string, max 120 chars, the headline shown on the image card",
                "alt_text": "string, describes the image card",
                "claims": ["each factual claim made, copied from the source"],
            },
        },
        ensure_ascii=False,
    )
    return call_json("write", settings.AI_MODEL_WRITE, system, user, max_tokens=1200)


def _samples(brand):
    text = (getattr(brand, "sample_posts", "") or "").strip()
    return text[:1500]


REVISE_ALLOWED = 300


def revise_post(*, category, brand, platform, limits, caption, hashtags, instruction, language, item):
    """Rewrite an existing draft following the owner's instruction, keeping the facts and the source."""
    system = (
        f"Category: {category.name}\n{category.system_prompt}\n\n"
        f"Brand voice: tone={brand.tone}; write in {language or brand.language}.\n"
        f"Banned words (never use): {', '.join(brand.banned_list()) or 'none'}."
    )
    user = json.dumps(
        {
            "task": "Rewrite the draft following the instruction. Keep every fact. Do not add facts that are not in the draft or source.",
            "instruction": instruction[:REVISE_ALLOWED],
            "platform": platform,
            "platform_style": limits["style"],
            "max_characters_for_caption_and_hashtags_together": limits["max_chars"] - 150,
            "max_hashtags": limits["max_hashtags"],
            "source": item,
            "draft": {"caption": caption, "hashtags": hashtags},
            "output_schema": {"caption": "string", "hashtags": ["#tag"]},
        },
        ensure_ascii=False,
    )
    return call_json("revise", settings.AI_MODEL_WRITE, system, user, max_tokens=1000)


def review_post(*, category, draft, item):
    model = settings.AI_MODEL_REVIEW_HIGH if category.risk_level == "high" else settings.AI_MODEL_REVIEW
    system = (
        "You are a strict, independent reviewer for a social media post. Check: (1) every factual claim is "
        "supported by the source, (2) nothing is copied verbatim from the source, (3) no hate, adult, political "
        "persuasion, medical/legal/financial advice or religious claims, (4) tone is appropriate."
    )
    user = json.dumps(
        {
            "task": 'Return {"verdict": "pass" or "fail", "reasons": ["..."], "risk_flags": ["..."]}',
            "category": category.name,
            "source": item,
            "draft": draft,
        },
        ensure_ascii=False,
    )
    return call_json("review", model, system, user, max_tokens=500)


# ---------------------------------------------------------------- dry run
def _dry_run(stage, user_json):
    """Deterministic placeholder output so the whole pipeline can be exercised without an API key."""
    payload = json.loads(user_json)
    if stage == "triage":
        return {"pick": payload["items"][0]["id"], "reason": "dry run"}
    if stage == "revise":
        d = payload.get("draft", {})
        return {"caption": f"[DRY RUN, revised: {payload.get('instruction', '')[:40]}] {d.get('caption', '')}"[:600], "hashtags": d.get("hashtags", [])}
    if stage == "write":
        src = payload.get("source", {})
        title = src.get("title", "today's topic")
        return {
            "caption": f"[DRY RUN, not generated by Claude] A short post about: {title}.",
            "hashtags": ["#dryrun"],
            "image_text": title[:110],
            "alt_text": f"Card showing: {title[:80]}",
            "claims": [],
        }
    return {"verdict": "pass", "reasons": ["dry run"], "risk_flags": []}
