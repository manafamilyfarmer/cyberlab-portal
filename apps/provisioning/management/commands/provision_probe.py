"""Run the B2 clone primitive synchronously in the worker and print the result.

Also runs an in-code guard self-check proving that disallowed VMIDs raise
BEFORE any API call is made (no network touched).
"""
import json

from django.core.management.base import BaseCommand

from apps.provisioning.pve import GuardError, ProxmoxClient
from apps.provisioning.tasks import provision_clone_primitive


def guard_selfcheck() -> dict:
    """Prove guards raise with NO API call. We swap _request for a sentinel
    that fails if reached, so a GuardError (not the sentinel) proves the guard
    fired first."""
    client = ProxmoxClient()

    def _no_network(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("GUARD BYPASSED: an API request was attempted!")

    client._request = _no_network

    cases = [
        ("clone(source=109)", lambda: client.clone(109, 9000, "x")),
        ("clone(source=200)", lambda: client.clone(200, 9000, "x")),
        ("destroy(106)", lambda: client.destroy(106)),
        ("get_config(200)", lambda: client.get_config(200)),
        ("get_status(110)", lambda: client.get_status(110)),
        ("destroy(8999_out_of_range)", lambda: client.destroy(8999)),
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
    help = "Run the B2 clone primitive (clone 151->9000, record, destroy) in-worker."

    def handle(self, *args, **options):
        self.stdout.write("=== GUARD SELF-CHECK (in-code, no API call) ===")
        guards = guard_selfcheck()
        self.stdout.write(json.dumps(guards, indent=2))

        self.stdout.write("\n=== PROVISION CLONE PRIMITIVE (real VM lifecycle) ===")
        result = provision_clone_primitive.run()  # run synchronously in-process
        result["guard_selfcheck"] = guards
        self.stdout.write(json.dumps(result, indent=2, default=str))
