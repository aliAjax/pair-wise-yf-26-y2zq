import base64
import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")


class DestructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "到期销毁档案", (date.today() + timedelta(days=30)).isoformat())
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(b"<record/>").decode()},
        ])
        self.copy = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.store.grant("owner", self.archive["id"], "archivist", "write")
        self.store.grant("owner", self.archive["id"], "auditor", "read")

    def tearDown(self):
        self.tmp.cleanup()

    def expire(self):
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE archives SET retention_until=? WHERE id=?",
                ((date.today() - timedelta(days=1)).isoformat(), self.archive["id"]),
            )

    def test_request_rejected_before_expiry(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", self.archive["id"], "保留期满销毁")
        self.assertEqual(ctx.exception.code, "retention_not_expired")

    def test_hold_blocks_request_and_late_hold_blocks_approval(self):
        self.expire()
        hold = self.store.register_hold("archivist", self.archive["id"], "诉讼保全")
        self.assertEqual(hold["status"], "active")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", self.archive["id"], "保留期满销毁")
        self.assertEqual(ctx.exception.code, "hold_active")
        revoked = self.store.revoke_hold("owner", hold["id"])
        self.assertEqual(revoked["status"], "revoked")
        with self.assertRaises(BusinessError) as ctx:
            self.store.revoke_hold("owner", hold["id"])
        self.assertEqual(ctx.exception.code, "hold_not_active")

        req = self.store.request_destruction("archivist", self.archive["id"], "保留期满销毁")
        self.assertEqual(req["status"], "pending")
        # 待审批期间新登记保全 → 审批失败
        late_hold = self.store.register_hold("owner", self.archive["id"], "审计核查")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(ctx.exception.code, "hold_active")
        self.store.revoke_hold("archivist", late_hold["id"])

        result = self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["files_removed"], 1)
        self.assertEqual(result["copies_removed"], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(ctx.exception.code, "invalid_state")

        # 内容与副本已清空，处置与保全变动记录保留
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(detail["files"], [])
        self.assertEqual(detail["copies"], [])
        status = self.store.archive_status("auditor", self.archive["id"])
        self.assertTrue(status["destroyed"])
        self.assertTrue(status["expired"])
        actions = [r["action"] for r in status["audit"]]
        for expected in ("hold.register", "hold.revoke", "destruction.request", "destruction.approve"):
            self.assertIn(expected, actions)
        self.assertEqual(len(status["holds"]), 2)
        self.assertEqual(status["destruction_requests"][0]["status"], "approved")

        # 销毁后禁止再写入内容或重复申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "a.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "already_destroyed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-b")
        self.assertEqual(ctx.exception.code, "already_destroyed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", self.archive["id"], "再次申请")
        self.assertEqual(ctx.exception.code, "already_destroyed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_hold("owner", self.archive["id"], "事后保全")
        self.assertEqual(ctx.exception.code, "already_destroyed")

    def test_roles_and_duplicate_request(self):
        self.expire()
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("auditor", self.archive["id"], "越权申请")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_hold("outsider", self.archive["id"], "越权保全")
        self.assertEqual(ctx.exception.status, 403)
        req = self.store.request_destruction("owner", self.archive["id"], "保留期满销毁")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("archivist", self.archive["id"], "重复申请")
        self.assertEqual(ctx.exception.code, "destruction_pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("owner", req["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("outsider", req["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
