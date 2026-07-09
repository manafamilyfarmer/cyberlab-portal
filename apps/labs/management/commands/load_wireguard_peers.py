"""Idempotently load per-student WireGuard peers from manifest.tsv (B4.4).

Reads ONLY ``manifest.tsv`` (student, tunnel_ip, kali_ip, client_pubkey — all
non-secret) from settings.WG_SECRETS_DIR and creates/updates one WireGuardPeer
per student. It NEVER opens/parses the private-key-bearing ``.conf`` files — it
only confirms each student's ``.conf`` EXISTS (so the download pointer is valid)
and records its filename as the pointer. No key material is ever read into the DB.

Consistency validated per row before anything is written (all-or-nothing in a
transaction):
  * the student (by username) and their StudentProfile exist,
  * kali_ip belongs to a VMInstance of THAT student's persistent per-student box
    (tunnel_ip ↔ kali_ip ↔ VMInstance), and
  * the student's ``<username>.conf`` is present in the secrets dir.

Idempotent: update_or_create keyed on the student, so re-running updates the 10
rows in place and never duplicates.
"""
import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import StudentProfile, User
from apps.labs.models import LabInstance, VMInstance, WireGuardPeer

_LIVE = (
    LabInstance.Status.PENDING,
    LabInstance.Status.RUNNING,
    LabInstance.Status.STOPPED,
)
_MANIFEST = "manifest.tsv"
_REQUIRED_COLS = ("student", "tunnel_ip", "kali_ip", "client_pubkey")


class Command(BaseCommand):
    help = (
        "Load/refresh per-student WireGuardPeer rows from manifest.tsv "
        "(non-secret; never reads the .conf key material). Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--secrets-dir",
            default=None,
            help="Override settings.WG_SECRETS_DIR (the staged, app-readable dir).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate the manifest and report, but write nothing.",
        )

    def handle(self, *args, **opts):
        secrets_dir = opts["secrets_dir"] or getattr(settings, "WG_SECRETS_DIR", None)
        if not secrets_dir:
            raise CommandError("WG_SECRETS_DIR is not configured.")
        manifest_path = os.path.join(secrets_dir, _MANIFEST)
        if not os.path.isfile(manifest_path):
            raise CommandError(f"manifest not found: {manifest_path}")

        rows = self._read_manifest(manifest_path)
        if not rows:
            raise CommandError(f"manifest is empty: {manifest_path}")

        planned = []          # (StudentProfile, VMInstance, row, config_ref)
        seen_tunnels = set()
        for lineno, row in rows:
            student = (row.get("student") or "").strip()
            tunnel_ip = (row.get("tunnel_ip") or "").strip()
            kali_ip = (row.get("kali_ip") or "").strip()
            pubkey = (row.get("client_pubkey") or "").strip()
            where = f"{_MANIFEST}:{lineno} (student={student!r})"

            if not (student and tunnel_ip and kali_ip and pubkey):
                raise CommandError(f"{where}: missing required field(s).")
            if tunnel_ip in seen_tunnels:
                raise CommandError(f"{where}: duplicate tunnel_ip {tunnel_ip}.")
            seen_tunnels.add(tunnel_ip)

            try:
                sp = StudentProfile.objects.get(user__username=student)
            except (User.DoesNotExist, StudentProfile.DoesNotExist):
                raise CommandError(f"{where}: no StudentProfile for username {student!r}.")

            # tunnel_ip ↔ kali_ip ↔ VMInstance: the kali_ip must be the leased IP
            # of a VMInstance belonging to THIS student's live per-student box.
            vm = (
                VMInstance.objects.filter(
                    ip__ip=kali_ip,
                    lab_instance__owner_student=sp,
                    lab_instance__provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
                    lab_instance__status__in=_LIVE,
                )
                .select_related("ip", "lab_instance")
                .first()
            )
            if vm is None:
                raise CommandError(
                    f"{where}: kali_ip {kali_ip} is not the leased IP of a live "
                    f"per-student box owned by {student}."
                )

            # Pointer = "<username>.conf"; confirm it EXISTS (never read it).
            config_ref = f"{student}.conf"
            conf_path = os.path.join(secrets_dir, config_ref)
            if not os.path.isfile(conf_path):
                raise CommandError(f"{where}: config file missing: {conf_path}")

            planned.append((sp, vm, tunnel_ip, kali_ip, pubkey, config_ref))

        if opts["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"dry-run OK: {len(planned)} peer(s) validated, nothing written."
                )
            )
            return

        created = updated = 0
        with transaction.atomic():
            for sp, vm, tunnel_ip, kali_ip, pubkey, config_ref in planned:
                _, was_created = WireGuardPeer.objects.update_or_create(
                    student=sp,
                    defaults={
                        "vm_instance": vm,
                        "tunnel_ip": tunnel_ip,
                        "kali_ip": kali_ip,
                        "client_pubkey": pubkey,
                        "config_secret_ref": config_ref,
                        "active": True,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        total = WireGuardPeer.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"load_wireguard_peers: {created} created / {updated} updated "
                f"(total peers now {total})"
            )
        )

    @staticmethod
    def _read_manifest(path):
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            missing = [c for c in _REQUIRED_COLS if c not in (reader.fieldnames or [])]
            if missing:
                raise CommandError(
                    f"manifest header missing column(s): {', '.join(missing)} "
                    f"(found: {reader.fieldnames})"
                )
            return [(i, row) for i, row in enumerate(reader, start=2)]
