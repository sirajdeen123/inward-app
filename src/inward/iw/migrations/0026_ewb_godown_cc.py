# 6-Nov-2026: the godown (clerk) mailbox is copied on the E-way bill Transporter ID update request. The list is created
# here with all its rows, so the seed (which only fills a missing list) does not leave it without the godown.

from django.db import migrations

ROWS = [("CFA Manager", "CC", ""), ("Admin Officer", "CC", ""), ("Godown", "CC", "coimbatoregodown@bergerindia.com")]


def forward(apps, schema_editor):
    NotificationRecipient = apps.get_model("core", "NotificationRecipient")
    for label, kind, default in ROWS:
        if NotificationRecipient.objects.filter(purpose="EWB_TRANSPORTER", role_label=label).exists():
            continue
        known = (NotificationRecipient.objects.filter(role_label=label).exclude(email="")
                 .values_list("email", flat=True).first() or default)
        NotificationRecipient.objects.create(purpose="EWB_TRANSPORTER", role_label=label, kind=kind, mandatory=True,
                                             email=known)


class Migration(migrations.Migration):

    dependencies = [
        ("iw", "0025_ewb_transporter"),
        ("core", "0009_ewb_transporter"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
