from django.core.management.base import BaseCommand, CommandError

from apps.content.models import AccountCategory
from apps.content.pipeline import run_generation
from apps.content.scheduling import create_manual_post
from apps.content.trends import ingest_category


class Command(BaseCommand):
    help = "Generate one post for a subscription id (use --ingest to fetch trends first). Handy for testing."

    def add_arguments(self, parser):
        parser.add_argument("subscription_id", type=int)
        parser.add_argument("--ingest", action="store_true")

    def handle(self, *args, **opts):
        try:
            sub = AccountCategory.objects.select_related("category", "account__workspace").get(pk=opts["subscription_id"])
        except AccountCategory.DoesNotExist:
            raise CommandError("No such subscription")
        if opts["ingest"]:
            self.stdout.write(f"fetched {ingest_category(sub.category, sub.region)} new trend items")
        post = create_manual_post(sub)
        post = run_generation(post.pk)
        self.stdout.write(f"post {post.pk}: {post.status}{' (dry run)' if post.dry_run else ''}")
        self.stdout.write(post.full_text() or post.error)
