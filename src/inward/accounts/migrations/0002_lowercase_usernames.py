"""User names are stored in lower case (sign-in lower-cases what is typed)."""
from django.db import migrations
from django.db.models.functions import Lower


def lower(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for u in User.objects.exclude(username=Lower("username")):
        if User.objects.filter(username=u.username.lower()).exclude(pk=u.pk).exists():
            continue   # a clash must be resolved by hand; leave it
        u.username = u.username.lower()
        u.save(update_fields=["username"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.RunPython(lower, migrations.RunPython.noop)]
