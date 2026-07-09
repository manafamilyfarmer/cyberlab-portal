"""B4.4 — per-student WireGuard config distribution: RBAC, audit, checksum.

Run (inside the web container, hermetic SQLite settings):
    python manage.py test apps.provisioning --settings=config.settings.test

These tests build FAKE fixture .conf files in a temp dir and point
WG_SECRETS_DIR at it via override_settings — no real secret is ever touched.
"""
import hashlib
import json
import os
import tempfile
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.provisioning import wgstatus

from apps.accounts.models import StudentProfile, User
from apps.accounts.permissions import IsWireGuardPeerOwner
from apps.audit.models import AuditLog
from apps.curriculum.models import Course, LabExercise, Module
from apps.labs.models import (
    IPLease,
    LabInstance,
    Role,
    VMInstance,
    WireGuardPeer,
)

WG_CONFIG_URL = "/api/my-lab/wireguard-config/"
MY_LAB_URL = "/api/my-lab/"


def _fake_conf(username, tunnel_ip, kali_ip):
    # Deliberately contains a SECRET-looking line so tests can prove it never
    # leaks into logs/audit. Bytes are unique per student for checksum tests.
    return (
        "[Interface]\n"
        f"# {username} -> Kali {kali_ip}\n"
        f"PrivateKey = FAKE-PRIVATE-KEY-{username}-DO-NOT-LEAK\n"
        f"Address = {tunnel_ip}/32\n\n"
        "[Peer]\n"
        "PublicKey = FAKESERVERPUBKEY000000000000000000000000000=\n"
        f"AllowedIPs = {kali_ip}/32\n"
    ).encode()


class WireGuardDistributionTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wg_dir = tempfile.mkdtemp(prefix="wgtest-")

    def setUp(self):
        # Shared curriculum spine for the per-student boxes.
        course = Course.objects.create(name="Track A", slug="track-a")
        module = Module.objects.create(course=course, code="A1", title="AppSec")
        self.exercise = LabExercise.objects.create(
            module=module, title="Kali box", slug="kali-box"
        )
        self.students = {}
        rows = ["student\ttunnel_ip\tkali_ip\tclient_pubkey"]
        for idx in (1, 2):
            username = f"student{idx:02d}"
            tunnel_ip = f"10.13.13.{149 + idx}"
            kali_ip = f"192.168.100.{149 + idx}"
            pubkey = f"PUBKEY{idx:02d}000000000000000000000000000000000000000="
            user = User.objects.create_user(
                username=username, password="x", role=User.Role.STUDENT
            )
            sp = StudentProfile.objects.create(user=user, student_index=idx)
            lease = IPLease.objects.create(ip=kali_ip, state=IPLease.State.LEASED)
            lab = LabInstance.objects.create(
                owner_student=sp,
                lab_exercise=self.exercise,
                provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
                status=LabInstance.Status.RUNNING,
            )
            vm = VMInstance.objects.create(
                lab_instance=lab, vmid=8999 + idx, role=Role.ATTACKER, ip=lease
            )
            lease.vm_instance = vm
            lease.save()
            # Fixture config on disk.
            body = _fake_conf(username, tunnel_ip, kali_ip)
            with open(os.path.join(self.wg_dir, f"{username}.conf"), "wb") as fh:
                fh.write(body)
            self.students[username] = {
                "user": user, "sp": sp, "vm": vm,
                "tunnel_ip": tunnel_ip, "kali_ip": kali_ip,
                "pubkey": pubkey, "body": body,
            }
            rows.append(f"{username}\t{tunnel_ip}\t{kali_ip}\t{pubkey}")
        with open(os.path.join(self.wg_dir, "manifest.tsv"), "w") as fh:
            fh.write("\n".join(rows) + "\n")

    def _load(self):
        from django.core.management import call_command
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            call_command("load_wireguard_peers")

    # ---- loader -----------------------------------------------------------
    def test_loader_creates_peers_and_is_idempotent(self):
        self._load()
        self.assertEqual(WireGuardPeer.objects.count(), 2)
        p = WireGuardPeer.objects.get(student=self.students["student01"]["sp"])
        self.assertEqual(p.tunnel_ip, "10.13.13.150")
        self.assertEqual(p.kali_ip, "192.168.100.150")
        self.assertEqual(p.config_secret_ref, "student01.conf")
        self.assertEqual(p.vm_instance_id, self.students["student01"]["vm"].id)
        # No private key or config text stored.
        for field in (p.client_pubkey, p.config_secret_ref):
            self.assertNotIn("PRIVATE", field.upper())
        # Re-run: still exactly 2, no duplicates.
        self._load()
        self.assertEqual(WireGuardPeer.objects.count(), 2)

    # ---- RBAC allow -------------------------------------------------------
    def test_student_downloads_own_config_bytes_identical(self):
        self._load()
        s = self.students["student01"]
        self.client.force_authenticate(user=s["user"])
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            resp = self.client.get(WG_CONFIG_URL, REMOTE_ADDR="196.12.41.100")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Disposition"],
            'attachment; filename="cyberlab-student01.conf"',
        )
        self.assertTrue(resp["Content-Type"].startswith("text/plain"))
        served = b"".join(resp.streaming_content)
        # Byte-identical to the source fixture (checksum compare).
        self.assertEqual(
            hashlib.sha256(served).hexdigest(),
            hashlib.sha256(s["body"]).hexdigest(),
        )
        # Bookkeeping updated.
        p = WireGuardPeer.objects.get(student=s["sp"])
        self.assertEqual(p.download_count, 1)
        self.assertIsNotNone(p.last_downloaded_at)
        self.assertIsNotNone(p.issued_at)

    # ---- RBAC deny: cross-student ----------------------------------------
    def test_cannot_reach_another_students_config(self):
        self._load()
        s1 = self.students["student01"]
        self.client.force_authenticate(user=s1["user"])
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            # No id param exists; attempts to smuggle student02 are ignored and
            # only student01's own bytes ever come back.
            for q in ("", "?student=2", "?peer=2", "?id=2"):
                resp = self.client.get(WG_CONFIG_URL + q, REMOTE_ADDR="196.12.41.100")
                self.assertEqual(resp.status_code, 200)
                served = b"".join(resp.streaming_content)
                self.assertEqual(served, s1["body"])
                self.assertNotIn(b"student02", served)
            # An id-style path does not resolve to another peer (no such route).
            resp = self.client.get(WG_CONFIG_URL + "2/")
            self.assertIn(resp.status_code, (403, 404))

    def test_permission_object_check_denies_other_owner(self):
        """The DRF permission itself refuses a peer owned by another student."""
        self._load()
        s1 = self.students["student01"]
        other_peer = WireGuardPeer.objects.get(student=self.students["student02"]["sp"])

        class _Req:
            pass

        req = _Req()
        req.user = s1["user"]
        perm = IsWireGuardPeerOwner()
        self.assertFalse(perm.has_object_permission(req, None, other_peer))
        own_peer = WireGuardPeer.objects.get(student=s1["sp"])
        self.assertTrue(perm.has_object_permission(req, None, own_peer))

    # ---- RBAC deny: staff -------------------------------------------------
    def test_staff_cannot_download_private_config(self):
        self._load()
        admin = User.objects.create_user(
            username="admin1", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(user=admin)
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            resp = self.client.get(WG_CONFIG_URL)
        self.assertEqual(resp.status_code, 403)

    # ---- audit ------------------------------------------------------------
    def test_download_writes_audit_without_secret(self):
        self._load()
        s = self.students["student01"]
        self.client.force_authenticate(user=s["user"])
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            b"".join(
                self.client.get(
                    WG_CONFIG_URL, REMOTE_ADDR="196.12.41.100"
                ).streaming_content
            )
        row = AuditLog.objects.filter(action="wireguard_config_download").latest("created_at")
        self.assertEqual(row.actor_id, s["user"].id)
        self.assertEqual(row.source_ip, "196.12.41.100")
        self.assertEqual(row.target_type, "WireGuardPeer")
        # No secret/key material anywhere in the audit detail.
        blob = json.dumps(row.detail)
        self.assertNotIn("PRIVATE", blob.upper())
        self.assertNotIn("FAKE-PRIVATE-KEY", blob)

    # ---- my-lab block -----------------------------------------------------
    def test_my_lab_includes_wireguard_block(self):
        self._load()
        s = self.students["student01"]
        self.client.force_authenticate(user=s["user"])
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            resp = self.client.get(MY_LAB_URL)
        self.assertEqual(resp.status_code, 200)
        wg = resp.data["wireguard"]
        self.assertTrue(wg["wg_config_available"])
        self.assertEqual(wg["tunnel_ip"], "10.13.13.150")
        self.assertEqual(wg["kali_ip"], "192.168.100.150")
        self.assertIn("wireguard-config", wg["download_url"])
        self.assertIsNone(wg["connected"])


class WireGuardStatusTests(APITestCase):
    """B4.5 — live WireGuard status poll: parser, freshness, cache, RBAC,
    resilience. No real vpn01 contact; fetch_raw() is mocked."""

    def setUp(self):
        cache.clear()
        course = Course.objects.create(name="Track A", slug="track-a")
        module = Module.objects.create(course=course, code="A1", title="AppSec")
        self.exercise = LabExercise.objects.create(
            module=module, title="Kali box", slug="kali-box"
        )
        self.wg_dir = tempfile.mkdtemp(prefix="wgstat-")
        self.students = {}
        rows = ["student\ttunnel_ip\tkali_ip\tclient_pubkey"]
        for idx in (1, 2):
            username = f"student{idx:02d}"
            tunnel_ip = f"10.13.13.{149 + idx}"
            kali_ip = f"192.168.100.{149 + idx}"
            pubkey = f"PUBKEY{idx:02d}000000000000000000000000000000000000000="
            user = User.objects.create_user(
                username=username, password="x", role=User.Role.STUDENT
            )
            sp = StudentProfile.objects.create(user=user, student_index=idx)
            lease = IPLease.objects.create(ip=kali_ip, state=IPLease.State.LEASED)
            lab = LabInstance.objects.create(
                owner_student=sp, lab_exercise=self.exercise,
                provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
                status=LabInstance.Status.RUNNING,
            )
            vm = VMInstance.objects.create(
                lab_instance=lab, vmid=8999 + idx, role=Role.ATTACKER, ip=lease
            )
            lease.vm_instance = vm
            lease.save()
            with open(os.path.join(self.wg_dir, f"{username}.conf"), "wb") as fh:
                fh.write(b"[Interface]\nPrivateKey = FAKE\n")
            self.students[username] = {
                "user": user, "sp": sp, "tunnel_ip": tunnel_ip,
                "kali_ip": kali_ip, "pubkey": pubkey,
            }
            rows.append(f"{username}\t{tunnel_ip}\t{kali_ip}\t{pubkey}")
        with open(os.path.join(self.wg_dir, "manifest.tsv"), "w") as fh:
            fh.write("\n".join(rows) + "\n")
        from django.core.management import call_command
        with override_settings(WG_SECRETS_DIR=self.wg_dir):
            call_command("load_wireguard_peers")
        from apps.labs.models import WireGuardPeer
        self.peer1 = WireGuardPeer.objects.get(student=self.students["student01"]["sp"])
        self.peer2 = WireGuardPeer.objects.get(student=self.students["student02"]["sp"])

    # ---- parser + freshness ----------------------------------------------
    def test_parser_and_freshness(self):
        now = 1_000_000
        dump = wgstatus.parse_dump(
            "PUBKEY01=\t%d\t100\t200\n"
            "PUBKEY02=\t0\t0\t0\n"
            "\n"
            "garbage-line\n" % (now - 10)
        )
        self.assertEqual(dump["PUBKEY01="], (now - 10, 100, 200))
        self.assertEqual(dump["PUBKEY02="], (0, 0, 0))
        # fresh handshake -> connected; stale + zero -> not.
        self.assertTrue(wgstatus.compute_connected(now - 10, now_epoch=now, freshness=180))
        self.assertFalse(wgstatus.compute_connected(now - 1000, now_epoch=now, freshness=180))
        self.assertFalse(wgstatus.compute_connected(0, now_epoch=now, freshness=180))

    # ---- poll writes cache; unknown pubkey ignored -----------------------
    def test_poll_caches_connected_and_ignores_unknown(self):
        now = 2_000_000
        p1 = self.students["student01"]["pubkey"]
        p2 = self.students["student02"]["pubkey"]
        raw = (
            f"{p1}\t{now - 5}\t10\t20\n"          # fresh -> connected
            f"{p2}\t{now - 5000}\t0\t0\n"         # stale -> not connected
            f"UNKNOWNPUBKEY=\t{now}\t1\t1\n"      # not a peer -> ignored
        )
        with mock.patch.object(wgstatus, "_now_epoch", return_value=now), \
             mock.patch.object(wgstatus, "fetch_raw", return_value=raw):
            summary = wgstatus.poll_and_cache()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["unknown_pubkeys"], 1)
        self.assertTrue(wgstatus.get_status(self.peer1.id)["connected"])
        self.assertIsNotNone(wgstatus.get_status(self.peer1.id)["last_handshake"])
        self.assertFalse(wgstatus.get_status(self.peer2.id)["connected"])

    # ---- flips true -> false on a later stale poll -----------------------
    def test_status_flips_false_after_disconnect(self):
        now = 3_000_000
        p1 = self.students["student01"]["pubkey"]
        with mock.patch.object(wgstatus, "_now_epoch", return_value=now), \
             mock.patch.object(wgstatus, "fetch_raw", return_value=f"{p1}\t{now-5}\t1\t1\n"):
            wgstatus.poll_and_cache()
        self.assertTrue(wgstatus.get_status(self.peer1.id)["connected"])
        # Later poll: same handshake epoch, but now is far past the freshness window.
        later = now + 10_000
        with mock.patch.object(wgstatus, "_now_epoch", return_value=later), \
             mock.patch.object(wgstatus, "fetch_raw", return_value=f"{p1}\t{now-5}\t1\t1\n"):
            wgstatus.poll_and_cache()
        self.assertFalse(wgstatus.get_status(self.peer1.id)["connected"])

    # ---- API: my-lab exposes connected + last_handshake, per-student -----
    def test_my_lab_connected_is_per_student(self):
        now = 4_000_000
        p1 = self.students["student01"]["pubkey"]
        p2 = self.students["student02"]["pubkey"]
        raw = f"{p1}\t{now-5}\t1\t1\n{p2}\t{now-9999}\t1\t1\n"
        with mock.patch.object(wgstatus, "_now_epoch", return_value=now), \
             mock.patch.object(wgstatus, "fetch_raw", return_value=raw):
            wgstatus.poll_and_cache()
        # student01 sees connected True (own), student02 sees False (own).
        self.client.force_authenticate(user=self.students["student01"]["user"])
        r1 = self.client.get("/api/my-lab/")
        self.assertEqual(r1.status_code, 200)
        self.assertIs(r1.data["wireguard"]["connected"], True)
        self.assertIsNotNone(r1.data["wireguard"]["last_handshake"])

        self.client.force_authenticate(user=self.students["student02"]["user"])
        r2 = self.client.get("/api/my-lab/")
        self.assertIs(r2.data["wireguard"]["connected"], False)

    # ---- resilience: vpn01 unreachable -> unknown, endpoint still 200 ----
    def test_unreachable_yields_unknown_no_exception(self):
        # No poll yet -> cache miss -> connected None.
        self.client.force_authenticate(user=self.students["student01"]["user"])
        r = self.client.get("/api/my-lab/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["wireguard"]["connected"])
        # A failing poll must not raise and must not write anything.
        with mock.patch.object(wgstatus, "fetch_raw", side_effect=RuntimeError("boom")):
            summary = wgstatus.poll_and_cache()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["updated"], 0)
        r2 = self.client.get("/api/my-lab/")
        self.assertEqual(r2.status_code, 200)
        self.assertIsNone(r2.data["wireguard"]["connected"])

    # ---- hardened ssh argv: strict host-key checking, no accept-new ------
    def test_ssh_argv_is_hardened_fixed_list(self):
        argv = wgstatus.build_ssh_argv()
        self.assertEqual(argv[0], "ssh")
        joined = " ".join(argv)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertNotIn("accept-new", joined)
        self.assertNotIn("StrictHostKeyChecking=no", joined)
        # user@host is a single argv element (no shell string).
        self.assertTrue(any(a.endswith("@192.168.100.7") for a in argv))
