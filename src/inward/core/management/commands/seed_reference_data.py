from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand

from inward.core.seed import seed_reference_data


class Command(BaseCommand):
    help = "Load roles, permissions, load/package types, business-line warehouses, flags, depot + CFA and rates."

    def add_arguments(self, parser):
        parser.add_argument("--go-live", required=True, help="YYYY-MM-DD: effective date for rates and the CFA")

    def handle(self, *args, go_live, **options):
        seed_reference_data(date.fromisoformat(go_live), depot_code=settings.INWARD["DEPOT_CODE"])
        self.stdout.write(self.style.SUCCESS(f"Reference data seeded (effective {go_live})."))
