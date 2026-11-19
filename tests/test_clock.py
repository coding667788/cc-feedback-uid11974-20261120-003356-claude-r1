"""逻辑时钟：事件驱动、clock==deadline 失效、同 tick 按事件序号判定。"""

import unittest

from helpers import BaseTestCase
from authenticator_committee.projection import verify


class TestClockBoundary(BaseTestCase):

    def test_clock_advances_one_per_accepted_event(self):
        self.init_v1()
        self.assertEqual(self.storage.current_clock(), 1)
        self.appoint("A1", deadline=10)
        self.assertEqual(self.storage.current_clock(), 2)
        # 被业务拒绝的命令不产生事件、不推进时钟
        before = self.storage.current_clock()
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])  # 门槛不足
        self.assertEqual(self.storage.current_clock(), before)

    def test_deadline_equality_expires(self):
        self.init_v1()
        self.appoint("A1", deadline=5)
        self.approve_v1_full("A1")
        # 前置事件已把时钟推到 5（init=1, appoint=2, 三条审批=3/4/5），
        # 即 clock 已与 deadline 相等：兑现必须判过期
        self.assertEqual(self.storage.current_clock(), 5)
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "EXPIRED")
        # inspect 视角同样标记失效
        appt = self.storage.get_appointment("A1")
        self.assertTrue(self.storage.current_clock() >= appt["deadline"])

    def test_just_before_deadline_still_fulfills(self):
        self.init_v1()
        self.appoint("A1", deadline=10)
        self.approve_v1_full("A1")   # 时钟到 5，距 deadline=10 仍有余量
        self.assertEqual(self.storage.current_clock(), 5)
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "OK")

    def test_at_deadline_fulfill_rejected_as_expired(self):
        self.init_v1()
        self.appoint("A1", deadline=5)
        self.approve_v1_full("A1")
        # 前置事件已把时钟推到与 deadline 相等的 tick
        self.assertEqual(self.storage.current_clock(), 5)
        r = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r), "EXPIRED")
        # inspect 视角同样标记失效
        appt = self.storage.get_appointment("A1")
        self.assertTrue(self.storage.current_clock() >= appt["deadline"])

    def test_same_tick_switch_and_expiry_ordered_by_seq(self):
        # 预约 deadline=10；在 tick=10 同一 tick 内先切换再尝试兑现，
        # 两个事件的先后由 seq 唯一确定。
        self.init_v1()
        self.appoint("A1", deadline=10)
        self.approve_v1_full("A1")
        self.apply([
            {"cmd": "define_config", "config": {
                "version": 2, "required_groups": 1, "chair_group": "G1",
                "members": {"G1": ["alice"]}}, "tick": 10},
            {"cmd": "switch", "version": 2, "tick": 10},
            {"cmd": "fulfill", "appointment_id": "A1", "tick": 10},
        ])
        events = [e for e in self.storage.all_events() if e["tick"] == 10]
        # 兑现因过期被拒，不会产生 authenticator_fulfilled 事件
        self.assertEqual(
            [e["event_type"] for e in events],
            ["config_defined", "config_switched"],
        )
        self.assertEqual([e["seq"] for e in events],
                         sorted(e["seq"] for e in events))
        # 切换在前使预约待转交，且 clock==deadline：过期优先于未转交
        receipt = self.storage.list_receipts()[-1]
        self.assertEqual(receipt["reason"], "EXPIRED")

    def test_same_tick_two_approvals_preserve_event_order(self):
        self.init_v1()
        self.appoint("A1", deadline=100)
        result = self.apply([
            {"cmd": "approve", "appointment_id": "A1",
             "witness": "alice", "content": "c1", "tick": 7},
            {"cmd": "approve", "appointment_id": "A1",
             "witness": "bob", "content": "c2", "tick": 7},
        ])
        self.assertTrue(all(r["accepted"] for r in result["receipts"]))
        seqs = [r["event_seq"] for r in result["receipts"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(self.storage.current_clock(), 7)

    def test_explicit_tick_equal_to_current_is_allowed(self):
        self.init_v1()
        r = self.apply([{"cmd": "tick", "tick": 1}])  # 当前时钟就是 1
        self.assertEqual(self.first_reason(r), "OK")

    def test_replay_preserves_clock(self):
        self.init_v1()
        self.appoint("A1")
        self.apply([{"cmd": "tick", "tick": 42}])
        self.reload_storage()
        self.assertEqual(self.storage.current_clock(), 42)
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])


if __name__ == "__main__":
    unittest.main()
