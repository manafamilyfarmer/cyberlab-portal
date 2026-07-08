from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("labs", "0004_vminstance_ip_applied"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="labinstance",
            constraint=models.UniqueConstraint(
                fields=("owner_student",),
                condition=models.Q(
                    ("provisioning_mode", "per_student"),
                    ("status__in", ["pending", "running", "stopped"]),
                ),
                name="uniq_active_per_student_box",
            ),
        ),
    ]
