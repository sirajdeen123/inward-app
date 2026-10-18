"""The daily IRR summary also goes to the Admin Officer (18-Oct-2026); the address is taken from the other lists."""
from django.db import migrations


def add(apps, schema_editor):
    R = apps.get_model("core", "NotificationRecipient")
    if not R.objects.filter(purpose="IRR_EXCEPTIONS").exists() or R.objects.filter(
            purpose="IRR_EXCEPTIONS", role_label="Admin Officer").exists():
        return
    known = R.objects.filter(role_label="Admin Officer").exclude(email="").values_list("email", flat=True).first() or ""
    R.objects.create(purpose="IRR_EXCEPTIONS", role_label="Admin Officer", kind="TO", mandatory=True, email=known)


class Migration(migrations.Migration):
    dependencies = [("irrecon", "0001_initial"), ("core", "0007_organisation_verified")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
