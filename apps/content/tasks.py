from celery import shared_task
from django.conf import settings

from apps.social.models import SocialAccount

from . import publishing, scheduling, sources, trends
from .models import AccountCategory, Category, FollowedSource


def enqueue(task, *args):
    """Run now when CELERY_EAGER is on (local dev, tests); otherwise send to the broker."""
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return task.apply(args=args)
    return task.delay(*args)


@shared_task
def ingest_trends():
    total = 0
    for source in FollowedSource.objects.filter(enabled=True, subscriptions__enabled=True).distinct():
        total += sources.refresh(source)
    for category in Category.objects.filter(is_active=True).exclude(source_type__in=["evergreen", "followed"]):
        # only fetch regions somebody actually subscribed to
        regions = set(AccountCategory.objects.filter(category=category, enabled=True).values_list("region", flat=True))
        for region in sorted(regions):
            total += trends.ingest_category(category, region)
    trends.purge_old()
    return total


@shared_task
def plan_slots():
    ids = scheduling.plan_slots()
    for post_id in ids:
        enqueue(generate_post_task, post_id)
    return len(ids)


@shared_task(bind=True, soft_time_limit=240)
def generate_post_task(self, post_id):
    from .pipeline import run_generation

    return run_generation(post_id).status


@shared_task
def publish_due_posts():
    sent = 0
    for post_id in publishing.due_post_ids():
        if publishing.claim_for_publishing(post_id):
            enqueue(publish_post_task, post_id)
            sent += 1
    return sent


@shared_task
def publish_post_task(post_id):
    return publishing.publish_post(post_id).status


@shared_task
def check_accounts():
    bad = 0
    for account in SocialAccount.objects.exclude(status="disabled"):
        if not publishing.check_account(account):
            bad += 1
    return bad


@shared_task
def collect_metrics():
    return publishing.collect_metrics()
