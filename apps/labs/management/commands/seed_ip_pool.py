"""Idempotently seed the reserved student IP pool 192.168.100.150–.249.

These are the student-clone addresses (B0 §5). Safe to re-run: get_or_create
means no duplicates. Prints created vs existing counts.
"""
from django.core.management.base import BaseCommand

from apps.labs.models import IPLease

POOL_PREFIX = "192.168.100."
POOL_START = 150
POOL_END = 249  # inclusive → 100 addresses


class Command(BaseCommand):
    help = "Seed the reserved student IP pool (192.168.100.150–.249) as free IPLeases."

    def handle(self, *args, **options):
        created = 0
        existing = 0
        for octet in range(POOL_START, POOL_END + 1):
            ip = f"{POOL_PREFIX}{octet}"
            _, was_created = IPLease.objects.get_or_create(
                ip=ip, defaults={"state": IPLease.State.FREE}
            )
            if was_created:
                created += 1
            else:
                existing += 1
        total = IPLease.objects.count()
        self.stdout.write(
            f"seed_ip_pool: {created} created / {existing} existing "
            f"(pool total now {total})"
        )
