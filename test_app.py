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

    def _expired_archive(self):
        return self.store.create_archive("owner", "到期档案", date.today().isoformat())

    def test_destruction_rejected_before_retention_expires(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", self.archive["id"])
        self.assertEqual(ctx.exception.code, "retention_active")
        self.assertEqual(ctx.exception.status, 422)

    def test_active_hold_blocks_request_and_revocation_unblocks(self):
        archive = self._expired_archive()
        hold = self.store.register_hold("owner", archive["id"], "诉讼保全")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", archive["id"])
        self.assertEqual(ctx.exception.code, "active_hold")
        self.store.revoke_hold("owner", hold["id"], "诉讼结束")
        req = self.store.request_destruction("owner", archive["id"])
        self.assertEqual(req["status"], "pending")

    def test_hold_registered_during_review_fails_approval_then_clears_on_success(self):
        archive = self._expired_archive()
        self.store.grant("owner", archive["id"], "archivist", "write")
        self.store.grant("owner", archive["id"], "auditor", "read")
        req = self.store.request_destruction("owner", archive["id"])
        # 待审批期间新登记的生效保全使审批失败，申请保持 pending
        self.store.register_hold("archivist", archive["id"], "审查中新增保全")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(ctx.exception.code, "active_hold_during_review")
        status = self.store.archive_status("owner", archive["id"])
        self.assertEqual(status["lifecycle"], "destruction_pending")
        # 撤销保全后审批通过
        hold_id = status["holds"][0]["id"]
        self.store.revoke_hold("owner", hold_id)
        result = self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(result["status"], "approved")
        status = self.store.archive_status("owner", archive["id"])
        self.assertEqual(status["lifecycle"], "destroyed")
        self.assertTrue(all(v["file_count"] == 0 for v in status["versions"]))
        self.assertTrue(all(v["copy_count"] == 0 for v in status["versions"]))
        # 保全变动与处置记录仍然保留
        actions = {e["action"] for e in status["audit"]}
        self.assertIn("hold.register", actions)
        self.assertIn("hold.revoke", actions)
        self.assertIn("destruction.request", actions)
        self.assertIn("destruction.approve", actions)
        # 已销毁档案拒绝再写入
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", archive["id"], [
                {"path": "new.txt", "content_b64": base64.b64encode(b"x").decode()}
            ])
        self.assertEqual(ctx.exception.code, "archive_destroyed")

    def test_destruction_roles_and_single_pending_request(self):
        archive = self._expired_archive()
        # auditor 不能申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("auditor", archive["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 未被授权的成员不能申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("archivist", archive["id"])
        self.assertEqual(ctx.exception.status, 403)
        self.store.grant("owner", archive["id"], "archivist", "write")
        req = self.store.request_destruction("archivist", archive["id"], "到期清退")
        # 同时只能有一条待审批申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_destruction("owner", archive["id"])
        self.assertEqual(ctx.exception.code, "destruction_pending")
        # auditor 无访问权不能审批
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("auditor", req["id"])
        self.assertEqual(ctx.exception.status, 403)
        # owner 不能代替 auditor 审批
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_destruction("owner", req["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
