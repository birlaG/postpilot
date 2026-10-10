from django.core.management.base import BaseCommand

from apps.content.models import Category

NEWS = "https://news.google.com/rss/search?q={q}&hl=en-{{cc}}&gl={{cc}}&ceid={{cc}}:en"

CATEGORIES = [
    dict(
        slug="trending-news", name="Trending news", icon="📰", source_type="rss",
        source_config={"feeds": ["https://news.google.com/rss?hl=en-{cc}&gl={cc}&ceid={cc}:en"], "limit": 20},
        risk_level="high", default_mode="approve", needs_source=True, freshness_hours=24,
        description="Top stories summarized in original words, with the source credited",
        system_prompt=(
            "Write a neutral news update. State what happened in 2 to 4 short sentences, then one line on why it matters. "
            "Credit the publisher if the source names one. DO: stay factual, use plain words. "
            "DON'T: speculate, take political sides, use sensational language, name suspects in crimes, or include "
            "casualty numbers unless they are in the source."
        ),
    ),
    dict(
        slug="trending-podcasts", name="Trending podcasts", icon="🎙️", source_type="apple_podcasts",
        source_config={"country": "{cc_lower}", "limit": 10}, risk_level="medium", default_mode="approve", needs_source=True,
        description="Podcasts climbing the charts, recommended with a takeaway",
        system_prompt=(
            "Recommend one podcast currently on the charts. Say who it is for and why it is worth a listen, based ONLY on "
            "the show name, host and genre you are given. DO: sound like a friend recommending. "
            "DON'T: invent episode details, quotes or guest names, and never reproduce transcripts."
        ),
    ),
    dict(
        slug="daily-jokes", name="Daily jokes", icon="😂", source_type="evergreen",
        source_config={"topics": ["office life", "Monday mornings", "students and exams", "cooking disasters", "traffic jams",
                                  "smartphones", "weddings", "coffee", "sports fans", "gym resolutions", "family WhatsApp groups",
                                  "online shopping", "work from home", "pets", "weather"]},
        risk_level="medium", default_mode="auto", description="A fresh, clean joke every day",
        system_prompt=(
            "Write one original, clean, light joke or funny observation on the topic given as the source title. "
            "DO: keep it short and relatable. DON'T: joke about religion, caste, gender, disability, nationality, politics "
            "or tragedies, and do not reuse famous jokes word for word."
        ),
    ),
    dict(
        slug="new-technology", name="New technology", icon="💡", source_type="hn",
        source_config={"limit": 15}, risk_level="medium", default_mode="approve", needs_source=True,
        description="What is new in tech, explained simply",
        system_prompt=(
            "Explain one tech story to a smart non-expert in 3 to 5 short lines: what it is, why it matters. "
            "DO: use an everyday analogy. DON'T: hype, make predictions, or state benchmark numbers not in the source."
        ),
    ),
    dict(
        slug="spiritual", name="Spiritual and devotional", icon="🕉️", source_type="evergreen",
        source_config={"topics": ["gratitude", "inner peace", "karma", "patience", "forgiveness", "mindfulness", "service to others",
                                  "morning blessing", "letting go", "humility", "faith in difficult times", "compassion"]},
        risk_level="high", default_mode="approve",
        disclaimer="",
        description="Gentle reflections and greetings, respectful of all faiths",
        system_prompt=(
            "Write a short, warm, universal reflection on the topic given. DO: be gentle and uplifting. "
            "DON'T: compare religions, claim miracles, cures or predictions, quote scripture verbatim unless you are "
            "certain it is accurate and public domain, or tell people to do anything for supernatural results."
        ),
    ),
    dict(
        slug="motivation", name="Motivation", icon="🔥", source_type="evergreen",
        source_config={"topics": ["starting before you feel ready", "consistency over intensity", "learning from failure", "small daily wins",
                                  "focus", "discipline", "resilience", "growth mindset", "courage", "saying no", "time management"]},
        risk_level="low", default_mode="auto", description="Original motivational lines and mini stories",
        system_prompt=(
            "Write an original, practical motivational post on the topic. DO: include one concrete action the reader can take today. "
            "DON'T: attribute quotes to real people, promise guaranteed results, or use cliches such as 'hustle culture'."
        ),
    ),
    dict(
        slug="facts", name="Facts and 'on this day'", icon="🧠", source_type="evergreen",
        source_config={"topics": ["the human body", "space", "oceans", "ancient civilizations", "animals", "inventions", "geography",
                                  "languages", "food history", "mathematics", "weather and climate"]},
        risk_level="medium", default_mode="approve", description="Surprising, true facts",
        system_prompt=(
            "Share one well-established, surprising fact on the topic. DO: only use facts you are highly confident are true and widely documented. "
            "DON'T: include precise statistics unless certain; if unsure, choose a different well-known fact."
        ),
    ),
    dict(
        slug="finance", name="Finance and markets", icon="📈", source_type="rss",
        source_config={"feeds": [NEWS.format(q="economy+OR+stock+market+OR+central+bank+when:1d")], "limit": 15},
        risk_level="high", default_mode="approve", needs_source=True, freshness_hours=24,
        disclaimer="Not investment advice. Do your own research.",
        description="Market and economy updates in plain language",
        system_prompt=(
            "Summarize one finance or economy headline in plain words for retail readers. DO: explain jargon. "
            "DON'T: recommend buying or selling anything, predict prices, name stock tips, or imply returns."
        ),
    ),
    dict(
        slug="health-tips", name="Health and wellness", icon="🌿", source_type="evergreen",
        source_config={"topics": ["drinking enough water", "posture at desk", "sleep routine", "walking", "screen breaks", "stretching",
                                  "mindful eating", "stress breathing", "sunlight and mood", "healthy snacks"]},
        risk_level="high", default_mode="approve",
        disclaimer="General information only. Talk to a doctor for personal advice.",
        description="Simple, safe daily wellness tips",
        system_prompt=(
            "Write one simple, widely accepted wellness tip on the topic. DO: keep it general and low risk. "
            "DON'T: name diseases, medicines, supplements, diets with numbers, or make any claim about treating or preventing illness."
        ),
    ),
    dict(
        slug="sports", name="Sports", icon="🏆", source_type="rss",
        source_config={"feeds": [NEWS.format(q="sports+when:1d")], "limit": 15},
        risk_level="medium", default_mode="approve", needs_source=True, freshness_hours=24,
        description="Match results and talking points",
        system_prompt=(
            "Write an energetic update on one sports story. DO: use only facts in the source. "
            "DON'T: invent scores, predict results, or insult players, teams or fans."
        ),
    ),
    dict(
        slug="followed-sources", name="Follow a podcast, channel or site", icon="🔗", source_type="followed",
        source_config={}, risk_level="medium", default_mode="approve", needs_source=True, freshness_hours=168,
        description="Turn new episodes, interviews and videos from shows you follow into posts, with credit",
        system_prompt=(
            "You are writing about someone else's episode, interview or video, using only its title and description. "
            "Write a highlights post: what it covers and the one or two takeaways worth knowing, in your own words. "
            "Name the show or channel from 'from' and say who is speaking only if the description names them. "
            "DO: invite people to watch or listen via the link. "
            "DON'T: claim you watched or listened, invent quotes, claims or guests, copy more than a few words of the "
            "description, or paraphrase it so closely that it replaces the original."
        ),
    ),
    dict(
        slug="ai-news", name="AI news", icon="🤖", source_type="rss",
        source_config={"feeds": [NEWS.format(q="artificial+intelligence+when:2d")], "limit": 15},
        risk_level="medium", default_mode="approve", needs_source=True, freshness_hours=48,
        description="What happened in AI this week, explained simply",
        system_prompt=(
            "Explain one AI headline to a non-expert in 3 to 4 short lines. DO: say why it matters to everyday people. "
            "DON'T: hype, predict the future, or state numbers or capabilities not in the source."
        ),
    ),
    dict(
        slug="business-startups", name="Business and startups", icon="🚀", source_type="rss",
        source_config={"feeds": [NEWS.format(q="startup+OR+entrepreneur+OR+business+when:2d")], "limit": 15},
        risk_level="medium", default_mode="approve", needs_source=True, freshness_hours=48,
        description="Founder lessons and business stories",
        system_prompt=(
            "Share one business or startup story and the lesson in it. DO: keep it practical. "
            "DON'T: give investment advice, promise results, or state funding amounts not in the source."
        ),
    ),
    dict(
        slug="entertainment", name="Movies and entertainment", icon="🎬", source_type="rss",
        source_config={"feeds": [NEWS.format(q="movies+OR+tv+series+OR+streaming+when:2d")], "limit": 15},
        risk_level="medium", default_mode="approve", needs_source=True, freshness_hours=48,
        description="Releases, trailers and talking points",
        system_prompt=(
            "Write a fun update about one release or entertainment story. DO: avoid spoilers. "
            "DON'T: spread gossip about private lives, invent plot details or release dates, or insult people."
        ),
    ),
    dict(
        slug="productivity", name="Productivity tips", icon="⏱️", source_type="evergreen",
        source_config={"topics": ["planning your day", "deep work", "inbox zero", "the two minute rule", "single tasking", "weekly review",
                                  "saying no to meetings", "habit stacking", "digital minimalism", "morning routine", "breaks that work"]},
        risk_level="low", default_mode="auto", description="Practical tips people can use today",
        system_prompt="Write one practical productivity tip on the topic with a concrete first step. DON'T make promises like 'double your output'.",
    ),
    dict(
        slug="travel", name="Travel ideas", icon="✈️", source_type="evergreen",
        source_config={"topics": ["budget travel", "solo travel", "packing light", "hidden hill stations", "street food trails", "train journeys",
                                  "weekend getaways", "travel photography", "festivals around the world", "road trip planning"]},
        risk_level="medium", default_mode="approve", description="Inspiring travel ideas and tips",
        system_prompt=(
            "Write an inspiring travel idea or tip on the topic. DO: stay general and evergreen. "
            "DON'T: quote prices, visa rules, opening times, safety claims or anything that changes often."
        ),
    ),
    dict(
        slug="food-recipes", name="Food and cooking", icon="🍲", source_type="evergreen",
        source_config={"topics": ["quick breakfasts", "one pot dinners", "street food at home", "meal prep", "snacks for guests", "festival sweets",
                                  "kitchen hacks", "tea and coffee", "leftover makeover", "salads that satisfy"]},
        risk_level="medium", default_mode="approve", description="Simple dishes and kitchen tips",
        disclaimer="",
        system_prompt=(
            "Share one simple dish idea or kitchen tip on the topic. DO: keep it short, with the main ingredients. "
            "DON'T: make health, weight-loss or allergy claims, or give exact food-safety temperatures."
        ),
    ),
]


class Command(BaseCommand):
    help = "Create or update the launch category recipes."

    def handle(self, *args, **opts):
        for spec in CATEGORIES:
            slug = spec.pop("slug")
            _, created = Category.objects.update_or_create(slug=slug, defaults=spec)
            spec["slug"] = slug
            self.stdout.write(f"{'created' if created else 'updated'} {slug}")
