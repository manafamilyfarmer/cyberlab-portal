from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_studentprofile_student_index"),
        ("labs", "0006_reapersighting"),
    ]

    operations = [
        migrations.CreateModel(
            name="WireGuardPeer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tunnel_ip", models.GenericIPAddressField(protocol="IPv4", unique=True)),
                ("kali_ip", models.GenericIPAddressField(protocol="IPv4")),
                ("client_pubkey", models.CharField(max_length=64)),
                ("config_secret_ref", models.CharField(max_length=255)),
                ("issued_at", models.DateTimeField(blank=True, null=True)),
                ("last_downloaded_at", models.DateTimeField(blank=True, null=True)),
                ("download_count", models.PositiveIntegerField(default=0)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "student",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wireguard_peer",
                        to="accounts.studentprofile",
                    ),
                ),
                (
                    "vm_instance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="wireguard_peers",
                        to="labs.vminstance",
                    ),
                ),
            ],
            options={
                "ordering": ("tunnel_ip",),
            },
        ),
    ]
