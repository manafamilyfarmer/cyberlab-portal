from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labs", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="labinstance",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("stopped", "Stopped"),
                    ("expired", "Expired"),
                    ("error", "Error"),
                    ("destroyed", "Destroyed"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
