from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labs", "0003_vminstance_vmid_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="vminstance",
            name="ip_applied",
            field=models.BooleanField(default=False),
        ),
    ]
