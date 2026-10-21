"""Go-live 22-Oct-2026: requests saved before the 'tracked' flag existed get the same rule as new ones - tracked
when still pending, raised on / after go-live, or linked to a GRN; otherwise kept for reference only."""
from datetime import date

from django.db import migrations

GO_LIVE = date(2026, 10, 22)


def apply(apps, schema_editor):
    Request = apps.get_model("rma", "Request")
    GrnLine = apps.get_model("rma", "GrnLine")
    with_grn = set(GrnLine.objects.exclude(request__isnull=True).values_list("request_id", flat=True))
    for r in Request.objects.filter(closed_at__isnull=True):
        keep = ("PENDING" in (r.status or "").upper() or (r.requested_date and r.requested_date >= GO_LIVE)
                or r.pk in with_grn)
        if r.tracked != keep:
            r.tracked = keep
            r.save(update_fields=["tracked"])


class Migration(migrations.Migration):
    dependencies = [("rma", "0002_tracked_and_auto_irr")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
