from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labs", "0002_labinstance_status_destroyed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vminstance",
            name="vmid",
            field=models.IntegerField(blank=True, null=True, unique=True),
        ),
    ]
