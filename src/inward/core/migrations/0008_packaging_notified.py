# 23-Oct-2026: packaging changes are mailed to the CFA Manager; every role may add / change packaging.

from django.db import migrations, models


def forward(apps, schema_editor):
    PackagingChange = apps.get_model("core", "PackagingChange")
    PackagingChange.objects.update(notified=True)          # history is not mailed - only changes from now on
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")
    if not Permission.objects.filter(code="SKU_PACKAGING_EDIT").exists():
        return                                              # a fresh database: the seed sets it
    for role in Role.objects.all():
        RolePermission.objects.get_or_create(role=role, permission_id="SKU_PACKAGING_EDIT", mode="GRANT")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_organisation_verified'),
        ('accounts', '0002_lowercase_usernames'),
    ]

    operations = [
        migrations.AddField(
            model_name='packagingchange',
            name='notified',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
