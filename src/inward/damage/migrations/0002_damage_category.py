"""Damage reasons become a master the supervisor can add to; the four first reasons are kept."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

FIRST = [("HANDLING", "Handling damage (labour)", True, False, 10), ("EXPIRY", "Expiry", False, False, 20),
         ("LEAKAGE", "Lid open / leakage", True, False, 30), ("OTHER", "Other", True, True, 90)]


def seed(apps, schema_editor):
    Cat = apps.get_model("damage", "DamageCategory")
    for code, name, photo, remarks, order in FIRST:
        Cat.objects.get_or_create(code=code, defaults={"name": name, "photo_required": photo,
                                                       "remarks_required": remarks, "sort_order": order})


class Migration(migrations.Migration):
    dependencies = [("damage", "0001_initial"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="DamageCategory",
            fields=[
                ("code", models.CharField(max_length=20, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=80, unique=True)),
                ("photo_required", models.BooleanField(default=True)),
                ("remarks_required", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "damage_category", "ordering": ["sort_order", "name"],
                     "verbose_name_plural": "damage categories"},
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
        migrations.AlterField(model_name="damageline", name="category",
                              field=models.ForeignKey(db_column="category", on_delete=django.db.models.deletion.PROTECT,
                                                      related_name="+", to="damage.damagecategory")),
    ]
