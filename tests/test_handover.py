"""委员会交接：跨组人员变更、共享组复用、负责人同一人禁令、只能转交一次。"""

import unittest

from helpers import BaseTestCase
from authenticator_committee.projection import verify


class TestHandover(BaseTestCase):

    def _pending_with_full_old_threshold(self):
        self.init_v1()
        self.appoint("A1")
        self.approve_v1_full("A1")
        self.define_switch_v2()
        # 切换后预约进入待转交，旧版本三组门槛仍满足
        appt = self.storage.get_appointment("A1")
        self.assertEqual(appt["status"], "PENDING_HANDOVER")

    def test_switch_moves_open_appointments_to_pending(self):
        self.init_v1()
        self.appoint("A1")
        self.appoint("A2", device="fp-2")
        self.define_switch_v2()
        for appt_id in ("A1", "A2"):
            self.assertEqual(
                self.storage.get_appointment(appt_id)["status"],
                "PENDING_HANDOVER",
            )
        # 已兑现预约不受切换影响
        self.init_v1()  # 幂等拒绝，不影响状态
        self.assertEqual(self.storage.active_version(), 2)

    def test_handover_requires_old_threshold_and_new_chair(self):
        self.init_v1()
        self.appoint("A1")
        self.define_switch_v2()
        # 旧门槛不满足
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "THRESHOLD_NOT_MET")

        # 补上旧门槛（显式 version=1），但缺新版本负责人确认
        self.apply([
            {"cmd": "approve", "appointment_id": "A1", "witness": "alice",
             "version": 1, "content": "v1"},
            {"cmd": "approve", "appointment_id": "A1", "witness": "bob",
             "version": 1, "content": "v1"},
            {"cmd": "approve", "appointment_id": "A1", "witness": "carol",
             "version": 1, "content": "v1"},
        ])
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "CHAIR_MISSING")

    def test_same_person_cannot_be_chair_on_both_sides(self):
        self._pending_with_full_old_threshold()
        # alice 同时是 v1 负责人见证人，又以 v2 负责人身份确认
        self.approve("A1", "alice", "v2-attest", version=2)
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "SAME_CHAIR_BOTH_SIDES")

    def test_different_chair_and_shared_group_completes_handover(self):
        self._pending_with_full_old_threshold()
        # dave 是 v2 负责人组里的另一个人；bob 所在 G2 是两边共享组
        self.approve("A1", "dave", "v2-attest", version=2)
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "OK")
        appt = self.storage.get_appointment("A1")
        self.assertEqual(appt["status"], "ACTIVE")
        self.assertEqual(appt["version"], 2)
        self.assertEqual(appt["handover_count"], 1)
        # 旧版本审批全部被取代，必须重新收集新版本见证
        self.assertTrue(
            all(a["superseded"] for a in
                self.storage.valid_approvals("A1", 1)) or
            self.storage.valid_approvals("A1", 1) == []
        )
        # 旧门槛满足但新版本尚无见证 -> 转交后不能立刻兑现
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "THRESHOLD_NOT_MET")

    def test_shared_group_can_serve_each_version_separately(self):
        self._pending_with_full_old_threshold()
        self.approve("A1", "dave", "v2", version=2)
        # bob 属于共享组 G2：在 v1 已见证，转交后在 v2 再见证一次，合法
        r = self.approve("A1", "bob", "v2", version=2)
        self.assertEqual(self.first_reason(r), "OK")
        self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        # v2 上 bob 的 G2 + dave 的 G1，还缺第三组 G4
        self.assertEqual(
            self.first_reason(self.apply([
                {"cmd": "fulfill", "appointment_id": "A1"}])),
            "THRESHOLD_NOT_MET",
        )
        r = self.approve("A1", "erin", "v2", version=2)
        self.assertEqual(self.first_reason(r), "OK")
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "OK")

    def test_appointment_can_only_be_handed_over_once(self):
        self.test_different_chair_and_shared_group_completes_handover()
        # 已转交、当前 ACTIVE：再要求转交 -> 不在待转交状态
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "HANDOVER_NOT_PENDING")

        # 再次切换让它重新进入待转交：次数约束拦截
        self.apply([
            {"cmd": "define_config", "config": {
                "version": 3, "required_groups": 1, "chair_group": "G1",
                "members": {"G1": ["alice", "dave"]}}},
            {"cmd": "switch", "version": 3},
        ])
        self.assertEqual(
            self.storage.get_appointment("A1")["status"], "PENDING_HANDOVER"
        )
        r = self.apply([{"cmd": "handover", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "ALREADY_HANDED_OVER")

    def test_handover_verifies_after_replay(self):
        self.test_different_chair_and_shared_group_completes_handover()
        self.reload_storage()
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])


if __name__ == "__main__":
    unittest.main()
