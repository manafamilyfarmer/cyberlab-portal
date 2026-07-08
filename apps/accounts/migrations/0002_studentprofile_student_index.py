from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentprofile",
            name="student_index",
            field=models.PositiveIntegerField(blank=True, null=True, unique=True),
        ),
    ]
