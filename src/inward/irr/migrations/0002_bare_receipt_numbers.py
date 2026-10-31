# 31-Oct-2026: a receipt saved in ERP without the reference text shows only the number in the IRR ('44514'); those
# rows had no receipt number, so the IW reconciliation could not find them (IW-2627-000026).

from django.db import migrations
from django.db.models import F


def forward(apps, schema_editor):
    IrrTransaction = apps.get_model("irr", "IrrTransaction")
    (IrrTransaction.objects.filter(type_key__in=["INT REQ INTR RCPT", "PO RECEIPT"], receipt_no="",
                                   trx_number__regex=r"^[0-9]+$")
     .update(receipt_no=F("trx_number")))


class Migration(migrations.Migration):

    dependencies = [
        ("irr", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
