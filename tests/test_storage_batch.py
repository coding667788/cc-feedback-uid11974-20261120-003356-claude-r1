"""存储与批处理：原子性、整批回滚、幂等重放、提交前故障注入、哈希链。"""

import unittest

from helpers import BaseTestCase, CFG_V1
from authenticator_committee.errors import EngineFault, InvalidBatch
from authenticator_committee.projection import verify


class TestBatchAtomicity(BaseTestCase):

    def test_invalid_batch_rolls_back_entirely(self):
        self.init_v1()
        seq_before = self.storage.last_event_seq()
        bad_batch = {
            "batch_id": "bad-1",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A1", "account": "acct-1",
                 "device_fp": "fp-1", "deadline": 100},
                {"cmd": "approve", "appointment_id": "A1"},  # 缺字段
            ],
        }
        with self.assertRaises(InvalidBatch):
            self.engine.apply_batch(bad_batch)
        # 没有任何事件、批次、回执残留
        self.assertEqual(self.storage.last_event_seq(), seq_before)
        self.assertIsNone(self.storage.get_batch("bad-1"))
        self.assertEqual(self.storage.list_receipts(batch_id="bad-1"), [])
        self.assertIsNone(self.storage.get_appointment("A1"))

    def test_business_rejections_still_publish_receipts(self):
        self.init_v1()
        result = self.apply([
            {"cmd": "appoint", "appointment_id": "A1", "account": "acct-1",
             "device_fp": "fp-1", "deadline": 100},
            # 门槛不足直接兑现 -> 合法业务拒绝
            {"cmd": "fulfill", "appointment_id": "A1"},
            # 不存在的预约
            {"cmd": "fulfill", "appointment_id": "NOPE"},
        ])
        self.assertEqual(result["summary"],
                         {"total": 3, "accepted": 1, "rejected": 2})
        self.assertEqual(
            [r["reason"] for r in result["receipts"]],
            ["OK", "THRESHOLD_NOT_MET", "APPOINTMENT_NOT_FOUND"],
        )
        # 批次已发布且可查询
        self.assertIsNotNone(self.storage.get_batch(result["batch_id"]))
        rejected = self.storage.list_receipts(rejected_only=True)
        self.assertEqual({r["reason"] for r in rejected},
                         {"THRESHOLD_NOT_MET", "APPOINTMENT_NOT_FOUND"})

    def test_idempotent_batch_replay(self):
        self.init_v1()
        batch = {
            "batch_id": "once",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A1", "account": "a",
                 "device_fp": "fp-1", "deadline": 100},
            ],
        }
        first = self.engine.apply_batch(batch)
        seq_after_first = self.storage.last_event_seq()
        second = self.engine.apply_batch(batch)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        # 重放不新增事件，回执内容稳定
        self.assertEqual(self.storage.last_event_seq(), seq_after_first)
        self.assertEqual(
            [r["reason"] for r in first["receipts"]],
            [r["reason"] for r in second["receipts"]],
        )

    def test_fault_before_commit_leaves_no_trace(self):
        self.init_v1()
        seq_before = self.storage.last_event_seq()
        batch = {
            "batch_id": "faulty",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A1", "account": "a",
                 "device_fp": "fp-1", "deadline": 100},
                {"cmd": "tick"},
            ],
        }
        with self.assertRaises(EngineFault):
            self.engine.apply_batch(batch, fault="before_commit")
        self.assertEqual(self.storage.last_event_seq(), seq_before)
        self.assertIsNone(self.storage.get_batch("faulty"))
        self.assertIsNone(self.storage.get_appointment("A1"))

    def test_retry_after_fault_succeeds_and_publishes(self):
        self.init_v1()
        batch = {
            "batch_id": "retry-me",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A1", "account": "a",
                 "device_fp": "fp-1", "deadline": 100},
            ],
        }
        with self.assertRaises(EngineFault):
            self.engine.apply_batch(batch, fault="before_commit")
        # 同一批次重试：此前没有发布，可以正常提交
        result = self.engine.apply_batch(batch)
        self.assertEqual(self.first_reason(result), "OK")
        self.assertIsNotNone(self.storage.get_appointment("A1"))
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])

    def test_hash_chain_tamper_detected(self):
        self.init_v1()
        self.appoint()
        # 直接篡改事件负载，模拟日志被改动
        self.storage.conn.execute(
            "UPDATE events SET payload = ? WHERE seq = (SELECT MIN(seq) FROM events)",
            ('{"tampered": true}',),
        )
        self.storage.commit()
        ok, report = verify(self.storage)
        self.assertFalse(ok)
        self.assertFalse(report["hash_chain_ok"])
        self.assertTrue(report["discrepancies"])

    def test_tick_cannot_go_backwards(self):
        self.init_v1()
        self.apply([{"cmd": "tick", "tick": 50}])
        result = self.apply([{"cmd": "tick", "tick": 49}])
        self.assertEqual(self.first_reason(result), "TICK_OUT_OF_ORDER")
        # 被拒绝的事件不推进时钟
        self.assertEqual(self.storage.current_clock(), 50)

    def test_structural_validators(self):
        # 未初始化
        with self.assertRaises(InvalidBatch):
            self.engine.apply_batch({"commands": []})
        self.init_v1()
        with self.assertRaises(InvalidBatch):
            self.engine.apply_batch({
                "batch_id": "x",
                "commands": [{"cmd": "unknown_cmd"}],
            })
        with self.assertRaises(InvalidBatch):
            self.engine.apply_batch({
                "batch_id": "y",
                "commands": [{"cmd": "appoint", "appointment_id": "A1",
                              "account": "a", "device_fp": "d", "deadline": -1}],
            })
        with self.assertRaises(InvalidBatch):
            self.engine.apply_batch({
                "batch_id": "z",
                "commands": [{"cmd": "init", "config": {
                    **CFG_V1, "version": 2}}],
            })

    def test_rebuild_state_after_restart(self):
        self.init_v1()
        self.appoint()
        self.approve_v1_full("A1")
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.reload_storage()
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])
        appt = self.storage.get_appointment("A1")
        self.assertEqual(appt["status"], "FULFILLED")
        self.assertEqual(report["replayed_summary"]["current_devices"],
                         {"fp-1": "acct-1"})


if __name__ == "__main__":
    unittest.main()
