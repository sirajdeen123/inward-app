from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iw", "0016_uat_round1")]
    operations = [migrations.AddField(model_name="sedline", name="excess_value",
                                      field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True))]
