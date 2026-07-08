import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labs", "0005_labinstance_uniq_active_per_student_box"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReaperSighting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vmid", models.PositiveIntegerField(unique=True)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("first_seen", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "ordering": ("vmid",),
            },
        ),
    ]
