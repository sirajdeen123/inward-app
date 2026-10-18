from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0006_document_cloud_path"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="organisation", name="verified", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="organisation", name="created_by",
                            field=models.ForeignKey(blank=True, null=True, on_delete=models.PROTECT, related_name="+",
                                                    to=settings.AUTH_USER_MODEL)),
    ]
