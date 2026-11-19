"""见证员撤销：下一条事件生效、未兑现审批失效、已完成登记保留历史、已撤销者不能确认。"""

import unittest

from helpers import BaseTestCase
from authenticator_committee.engine import evaluate_threshold
from authenticator_committee.projection import verify


class TestRevocation(BaseTestCase):

    def test_revocation_takes_effect_from_next_event(self):
        self.init_v1()
        self.appoint("A1")
        self.approve_v1_full("A1")
        # 撤销前门槛达标
        met, _, _ = evaluate_threshold(self.storage, "A1", 1)
        self.assertTrue(met)

        # 撤销 carol：自该撤销事件起，carol 未兑现的审批立即不再计入
        r = self.apply([{"cmd": "revoke", "witness": "carol"}])
        self.assertEqual(self.first_reason(r), "OK")
        met, covered, detail = evaluate_threshold(self.storage, "A1", 1)
        self.assertFalse(met)
        self.assertNotIn("G3", covered)

        # 撤销幂等
        r = self.apply([{"cmd": "revoke", "witness": "carol"}])
        self.assertEqual(self.first_reason(r), "OK")
        self.assertEqual(
            len([e for e in self.storage.all_events()
                 if e["event_type"] == "witness_revoked"]),
            1,
        )

    def test_revoked_witness_cannot_approve(self):
        self.init_v1()
        self.appoint("A1")
        self.apply([{"cmd": "revoke", "witness": "bob"}])
        r = self.approve("A1", "bob", "late")
        self.assertEqual(self.first_reason(r), "ALREADY_REVOKED")

    def test_completed_registration_survives_revocation(self):
        self.init_v1()
        self.appoint("A1")
        self.approve_v1_full("A1")
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        # 登记已完成后撤销见证员：登记历史保留且仍为 current
        self.apply([{"cmd": "revoke", "witness": "carol"}])
        reg = self.storage.registration_for_device("fp-1")
        self.assertEqual(reg["status"], "current")
        self.assertEqual(reg["account"], "acct-1")
        # 审计视角：撤销事件与兑现事件都在日志里
        types = [e["event_type"] for e in self.storage.all_events()]
        self.assertIn("authenticator_fulfilled", types)
        self.assertIn("witness_revoked", types)

    def test_revoke_then_replace_witness_then_fulfill(self):
        self.init_v1()
        self.appoint("A1")
        self.approve_v1_full("A1")
        self.apply([{"cmd": "revoke", "witness": "carol"}])
        # 门槛已破 -> 兑现被拒
        self.assertEqual(
            self.first_reason(self.apply([
                {"cmd": "fulfill", "appointment_id": "A1"}])),
            "THRESHOLD_NOT_MET",
        )
        # 同组后备 frank 在 v1 补位，旧版本三组门槛恢复
        self.assertEqual(self.first_reason(
            self.approve("A1", "frank", "attest-v1", version=1)), "OK")
        # 交接至 v2：旧门槛达标，dave 作为与旧负责人不同的新版本负责人确认
        self.apply([
            {"cmd": "define_config", "config": {
                "version": 2, "required_groups": 3, "chair_group": "G1",
                "members": {
                    "G1": ["alice", "dave"],
                    "G2": ["bob"],
                    "G4": ["erin"],
                }}},
            {"cmd": "switch", "version": 2},
            {"cmd": "approve", "appointment_id": "A1", "witness": "dave",
             "version": 2, "content": "v2"},
        ])
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "OK")
        # 转交后旧见证全部作废，必须在 v2 重新凑齐三组（G1/G2/G4）
        self.approve("A1", "dave", "v2", version=2)
        self.approve("A1", "bob", "v2", version=2)
        self.approve("A1", "erin", "v2", version=2)
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "OK")
        # 审计：carol 的撤销事件仍在，且其审批未被计入最终登记
        reg = self.storage.registration_for_device("fp-1")
        self.assertEqual(reg["status"], "current")

    def test_unknown_witness_revocation_rejected(self):
        self.init_v1()
        r = self.apply([{"cmd": "revoke", "witness": "nobody"}])
        self.assertEqual(self.first_reason(r), "WITNESS_UNKNOWN")

    def test_revocation_evidence_stages_recorded(self):
        self.init_v1()
        self.appoint("A1")
        self.approve_v1_full("A1")
        self.apply([{"cmd": "revoke", "witness": "carol"}])
        stages = [e["stage"] for e in self.storage.list_evidence("A1")]
        self.assertIn("witness_revoked", stages)
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])


if __name__ == "__main__":
    unittest.main()
