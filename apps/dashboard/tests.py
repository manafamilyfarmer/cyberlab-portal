"""B6.4 — instructor console: the staff gate, batch scoping, and data honesty.

Run (inside the web container, hermetic SQLite settings):
    python manage.py test apps.dashboard --settings=config.settings.test

The gate these tests pin down is the one the rest of the staff surface uses:
admin/instructor ROLE **and** a verified TOTP device this session. There is no
weaker `is_staff` path into the console — if one is ever added, test_student_*
and test_instructor_without_mfa_* below should fail.
"""
from django.core.cache import cache
from django.test import TestCase
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import InstructorProfile, StudentProfile, User
from apps.curriculum.models import Batch, Course, LabExercise, Module
from apps.labs.models import IPLease, LabInstance, Role, VMInstance, WireGuardPeer
from apps.provisioning import wgstatus

CONSOLE_URL = "/instructor/"


class InstructorConsoleTests(TestCase):
    """One instructor, two batches' worth of students, so scoping is provable:
    the console must show MY student and never the other instructor's."""

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name="Course", slug="course")
        module = Module.objects.create(course=cls.course, code="M0", title="M0")
        cls.exercise = LabExercise.objects.create(
            module=module, title="Recon", slug="recon"
        )

        # Mine
        cls.instructor_user = User.objects.create_user(
            username="inst", password="x", role=User.Role.INSTRUCTOR
        )
        cls.instructor = InstructorProfile.objects.create(user=cls.instructor_user)
        cls.student_user = User.objects.create_user(
            username="stu", password="x", role=User.Role.STUDENT,
            first_name="Ada", last_name="Lovelace",
        )
        cls.student = StudentProfile.objects.create(
            user=cls.student_user, student_index=1
        )
        cls.batch = Batch.objects.create(
            course=cls.course, instructor=cls.instructor, name="My-Batch"
        )
        cls.batch.students.add(cls.student)

        # Someone else's — must never appear in MY console.
        other_instructor = InstructorProfile.objects.create(
            user=User.objects.create_user(
                username="inst2", password="x", role=User.Role.INSTRUCTOR
            )
        )
        cls.other_student_user = User.objects.create_user(
            username="stu2", password="x", role=User.Role.STUDENT
        )
        cls.other_student = StudentProfile.objects.create(
            user=cls.other_student_user, student_index=2
        )
        other_batch = Batch.objects.create(
            course=cls.course, instructor=other_instructor, name="Other-Batch"
        )
        other_batch.students.add(cls.other_student)

        # My student's real box + peer.
        lease = IPLease.objects.create(ip="192.168.100.150", state=IPLease.State.LEASED)
        cls.box = LabInstance.objects.create(
            owner_student=cls.student,
            lab_exercise=cls.exercise,
            status=LabInstance.Status.RUNNING,
            provisioning_mode=LabInstance.ProvisioningMode.PER_STUDENT,
        )
        cls.vm = VMInstance.objects.create(
            lab_instance=cls.box, vmid=9000, hostname="s01-kali-9000",
            ip=lease, role=Role.ATTACKER, proxmox_status="running",
        )
        cls.peer = WireGuardPeer.objects.create(
            student=cls.student, vm_instance=cls.vm,
            tunnel_ip="10.13.13.150", kali_ip="192.168.100.150",
            client_pubkey="PUBKEY-STU-1", config_secret_ref="stu.conf",
        )

    def setUp(self):
        cache.clear()

    # -- helpers ---------------------------------------------------------- #

    def _verify_mfa(self, user):
        """Give ``user`` a confirmed TOTP device and mark the CLIENT's session as
        OTP-verified — the same session key django_otp's own login() sets."""
        device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session.save()
        return device

    def _login_staff(self):
        self.client.force_login(self.instructor_user)
        self._verify_mfa(self.instructor_user)

    # -- the gate --------------------------------------------------------- #

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(CONSOLE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_student_is_denied(self):
        """A student must not be able to load the console — even with a session."""
        self.client.force_login(self.student_user)
        resp = self.client.get(CONSOLE_URL)
        self.assertEqual(resp.status_code, 403)

    def test_student_denied_on_student_detail(self):
        self.client.force_login(self.student_user)
        resp = self.client.get(f"/instructor/student/{self.student.id}/")
        self.assertEqual(resp.status_code, 403)

    def test_instructor_without_mfa_is_denied(self):
        """Role alone is not enough: the staff MFA gate applies to the console
        exactly as it does to every other staff page."""
        self.client.force_login(self.instructor_user)
        resp = self.client.get(CONSOLE_URL)
        self.assertEqual(resp.status_code, 403)

    def test_instructor_with_mfa_is_allowed(self):
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertEqual(resp.status_code, 200)

    # -- real data + scoping ---------------------------------------------- #

    def test_roster_shows_my_student_with_real_values(self):
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        body = resp.content.decode()
        self.assertContains(resp, "stu")
        self.assertIn("10.13.13.150", body)      # real tunnel IP
        self.assertIn("192.168.100.150", body)   # real box IP
        self.assertIn("AL", body)                # initials from the real name

    def test_roster_excludes_other_instructors_students(self):
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertNotContains(resp, "stu2")

    def test_student_detail_404s_for_out_of_scope_student(self):
        """Out of scope is indistinguishable from nonexistent (object isolation)."""
        self._login_staff()
        resp = self.client.get(f"/instructor/student/{self.other_student.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_student_detail_renders_my_student(self):
        self._login_staff()
        resp = self.client.get(f"/instructor/student/{self.student.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "10.13.13.150")

    # -- pills tell the truth --------------------------------------------- #

    def test_vpn_pill_unknown_when_poller_cache_is_empty(self):
        """No cache entry => "Unknown", never "Offline". A dead poller is not a
        disconnection (B4.5 semantics carried into the roster)."""
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertContains(resp, "Unknown")
        self.assertNotContains(resp, ">Offline<")

    def test_vpn_pill_connected_reflects_poller_cache(self):
        cache.set(
            wgstatus.cache_key(self.peer.id),
            {"connected": True, "last_handshake": "2026-07-17T10:00:00+00:00"},
            300,
        )
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertContains(resp, "Connected")

    def test_tiles_count_real_rows(self):
        cache.set(
            wgstatus.cache_key(self.peer.id),
            {"connected": True, "last_handshake": None},
            300,
        )
        self._login_staff()
        tiles = self.client.get(CONSOLE_URL).context["tiles"]
        self.assertEqual(tiles["students_total"], 1)
        self.assertEqual(tiles["connected_now"], 1)
        self.assertEqual(tiles["boxes_running"], 1)
        self.assertEqual(tiles["to_review"], 0)

    def test_no_fabricated_isolation_alert_count(self):
        """The portal has no isolation-alert source; the tile must stay numberless
        so it can never be misread as a real zero."""
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertNotIn("isolation_alerts", resp.context["tiles"])
        self.assertContains(resp, "Not wired up")

    # -- nav --------------------------------------------------------------- #

    def test_nav_link_hidden_from_students(self):
        self.client.force_login(self.student_user)
        resp = self.client.get("/my-lab/")
        self.assertNotContains(resp, "Instructor console", status_code=200)

    def test_nav_link_shown_to_staff(self):
        self._login_staff()
        resp = self.client.get(CONSOLE_URL)
        self.assertContains(resp, "Instructor console")
