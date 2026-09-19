"""审计哈希链测试。"""

from __future__ import annotations

import sqlite3

from tests.base import EBRTestCase
from ebr.audit import AuditTrail
from ebr.db import connect
from ebr.errors import AuditChainBrokenError


class AuditChainTest(EBRTestCase):
    def test_chain_verifies_on_empty_and_after_writes(self):
        audit = AuditTrail()
        self.assertTrue(audit.verify_chain()["ok"])
        self.make_batch("BX1")
        self.make_batch("BX2")
        result = audit.verify_chain()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["entries_checked"], 2)

    def test_tampered_reason_breaks_chain(self):
        self.make_batch("BX1")
        conn = connect(self.db_file)
        conn.execute("DROP TRIGGER trg_audit_no_update")
        conn.execute("UPDATE audit_log SET reason = 'hacked' WHERE id = 1")
        conn.commit()
        conn.close()
        with self.assertRaises(AuditChainBrokenError):
            AuditTrail().verify_chain()

    def test_deleted_entry_breaks_chain(self):
        self.make_batch("BX1")
        self.make_batch("BX2")
        conn = connect(self.db_file)
        conn.execute("DROP TRIGGER trg_audit_no_delete")
        conn.execute("DELETE FROM audit_log WHERE id = 1")
        conn.commit()
        conn.close()
        with self.assertRaises(AuditChainBrokenError):
            AuditTrail().verify_chain()

    def test_triggers_block_update_and_delete(self):
        self.make_batch("BX1")
        conn = connect(self.db_file)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET action = 'X' WHERE id = 1")
            conn.commit()
        conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log WHERE id = 1")
            conn.commit()
        conn.close()

    def test_denied_and_blocked_actions_are_logged(self):
        # 越权操作被拒也入链
        from ebr.errors import PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            service_release_by_analyst(self)
        logs = AuditTrail().list(entity_type="authorization")
        self.assertTrue(any(l["result"] == "DENIED" for l in logs))


def service_release_by_analyst(case):
    from ebr import service
    case.make_batch("BX9")
    return service.release_batch(actor=case.user("analyst"), batch_no="BX9",
                                 reason="x")
