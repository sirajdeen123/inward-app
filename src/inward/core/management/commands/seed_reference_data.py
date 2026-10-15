from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from inward.core.seed import seed_reference_data


class Command(BaseCommand):
    help = "Load roles, permissions, load/package types, business-line warehouses, flags, depot + CFA and rates."

    def add_arguments(self, parser):
        parser.add_argument("--go-live", help="YYYY-MM-DD: effective date for rates and the CFA (first run only)")

    def handle(self, *args, go_live=None, **options):
        from inward.core.models import DepotOperator
        if go_live is None and not DepotOperator.objects.exists():
            raise CommandError("First run: --go-live YYYY-MM-DD is required")
        seed_reference_data(date.fromisoformat(go_live) if go_live else date.today(),
                            depot_code=settings.INWARD["DEPOT_CODE"])
        self.stdout.write(self.style.SUCCESS("Reference data up to date."))
