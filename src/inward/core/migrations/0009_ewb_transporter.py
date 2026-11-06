# 6-Nov-2026: the CFA's Transporter ID for the E-way bill of an outbound transhipment, kept on the depot operator.

from django.db import migrations, models


def forward(apps, schema_editor):
    DepotOperator = apps.get_model("core", "DepotOperator")
    DepotOperator.objects.filter(cfa_name__icontains="RAINBOW", transporter_id="").update(
        transporter_id="33AADPZ7939E1ZC", transporter_name="RAINBOW LOGISTICS")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_packaging_notified'),
    ]

    operations = [
        migrations.AddField(
            model_name='depotoperator',
            name='transporter_id',
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name='depotoperator',
            name='transporter_name',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
