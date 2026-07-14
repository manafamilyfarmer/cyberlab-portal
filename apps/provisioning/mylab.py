"""Single source of truth for "what is MY lab?" — shared by the JSON API and
the /my-lab/ HTML page (B6.3).

Same guarantee as apps/dashboard/scoping.py: rather than re-implementing the
student scoping (which would risk the template showing something the API would
refuse), we drive the very same ``MyLabViewSet`` the JSON endpoint uses and read
its own helpers with the live request bound. Whatever GET /api/my-lab/ would
return for this user is exactly what the page renders — no more, no less.

RBAC note: every helper on the viewset resolves the box/peer from
``request.user``'s StudentProfile. There is NO id/query parameter anywhere in
this path, so there is nothing a client could tamper with to reach another
student's lab. The page inherits that property by construction, not by a second
copy of the rule.
"""
from .api import MyLabViewSet


def _bound_viewset(request):
    """A MyLabViewSet acting as if it were serving ``request`` (see scoping.py)."""
    vs = MyLabViewSet()
    vs.request = request
    vs.action = "list"
    vs.kwargs = {}
    vs.format_kwarg = None
    return vs


def my_lab_context(request):
    """Return the my-lab payload for the REQUESTING student.

    Mirrors MyLabViewSet.list(): the same peer, the same WireGuard block (incl.
    the B4.5 cached live status), and the same box instance. Returns a dict:

        {"box": LabInstance|None, "vm": VMInstance|None,
         "machine_ip": str|None, "wireguard": {...}}

    ``box`` is None when nothing is provisioned yet — the API answers 404 there;
    the page renders an honest empty state instead.
    """
    vs = _bound_viewset(request)
    peer = vs._my_peer(request.user)
    wg = vs._wg_block(request, peer)
    box = vs._my_box(request.user)

    # The per-student box is a single Kali VM (B3 Step 1). Take the first VM as
    # "your machine"; if the shape ever grows, the template still shows the box.
    vm = None
    if box is not None:
        vm = box.vms.select_related("ip").first()

    # Resolve the address here rather than in the template: VMInstance.ip is an
    # FK to IPLease, so rendering it directly would print the lease object. The
    # peer's kali_ip is the same address seen from the tunnel and is what the
    # student actually connects to, so it wins; the lease is the fallback for a
    # box that has no WireGuard peer yet. Mirrors the API serializer's get_ip().
    machine_ip = wg.get("kali_ip")
    if not machine_ip and vm is not None and vm.ip_id:
        machine_ip = str(vm.ip.ip)

    return {"box": box, "vm": vm, "machine_ip": machine_ip, "wireguard": wg}
