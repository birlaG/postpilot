from .base import NotImplementedPublisher, PublishError, Publisher  # noqa: F401
from .linkedin import LinkedInPublisher
from .meta import FacebookPublisher, InstagramPublisher
from .telegram import TelegramPublisher
from .xtwitter import XPublisher
from .youtube import YouTubePublisher

_REGISTRY = {
    "telegram": TelegramPublisher(),
    "facebook": FacebookPublisher(),
    "instagram": InstagramPublisher(),
    "youtube": YouTubePublisher(),
    "x": XPublisher(),
    "linkedin": LinkedInPublisher(),
}


def get_publisher(platform: str) -> Publisher:
    try:
        return _REGISTRY[platform]
    except KeyError:
        raise PublishError(f"Unknown platform {platform}", retryable=False)


def implemented_platforms():
    return [p for p, pub in _REGISTRY.items() if pub.implemented]
