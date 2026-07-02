"""Run the B2 Step 2 power-lifecycle probe synchronously in the worker and print
the structured result.

Also runs an in-code guard self-check proving the NEW power ops (start/stop/
shutdown) refuse disallowed VMIDs BEFORE any API call is made (no network).
"""
import json

from django.core.management.base import BaseCommand

from apps.provisioning.pve import GuardError, ProxmoxClient
from apps.provisioning.tasks import provision_lifecycle_probe


def guard_selfcheck() -> dict:
    """Swap _request for a sentinel that fails if reached, so a GuardError (not
    the sentinel) proves the guard fired before any network call."""
    client = ProxmoxClient()

    def _no_network(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("GUARD BYPASSED: an API request was attempted!")

    client._request = _no_network

    cases = [
        ("start(109)", lambda: client.start(109)),
        ("stop(106)", lambda: client.stop(106)),
        ("shutdown(110)", lambda: client.shutdown(110)),
        ("clone(source=200)", lambda: client.clone(200, 9000, "x")),
        ("wait_status(200)", lambda: client.wait_status(200, "running")),
        ("guest_ping(106)", lambda: client.guest_ping(106)),
    ]
    results = {}
    for label, fn in cases:
        try:
            fn()
            results[label] = "FAIL: no exception raised"
        except GuardError as exc:
            results[label] = f"PASS: GuardError ({exc})"
        except AssertionError as exc:
            results[label] = f"FAIL: {exc}"
    results["all_pass"] = all(v.startswith("PASS") for v in results.values())
    return results


class Command(BaseCommand):
    help = "Run the B2 Step 2 power-lifecycle probe (clone->start->stop->destroy) in-worker."

    def handle(self, *args, **options):
        self.stdout.write("=== GUARD SELF-CHECK (in-code, no API call) ===")
        guards = guard_selfcheck()
        self.stdout.write(json.dumps(guards, indent=2))

        self.stdout.write("\n=== PROVISION LIFECYCLE PROBE (real VM power lifecycle) ===")
        result = provision_lifecycle_probe.run()
        result["guard_selfcheck"] = guards
        self.stdout.write(json.dumps(result, indent=2, default=str))
