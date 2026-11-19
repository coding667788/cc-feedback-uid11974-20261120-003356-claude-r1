"""两进程并发提交：同一存储上不能重复兑现、不能产生部分记录。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import BaseTestCase, CFG_V1, write_json
from authenticator_committee.projection import verify
from authenticator_committee.storage import Storage

WORKER = Path(__file__).with_name("_concurrent_worker.py")


def _fulfill_only_batch(batch_id, appt_id):
    # 竞争只发生在兑现环节：预约与见证已在公共库中预先就绪，
    # 两个进程同时提交各自的兑现，争同一设备指纹。
    return {
        "batch_id": batch_id,
        "commands": [
            {"cmd": "fulfill", "appointment_id": appt_id},
        ],
    }


class TestConcurrentProcesses(BaseTestCase):

    def test_two_processes_race_same_device(self):
        # 先在主进程把初始化、两枚预约及全套见证准备好并提交
        self.engine.apply_batch({
            "batch_id": "init",
            "commands": [{"cmd": "init", "config": CFG_V1}],
        })
        setup = {
            "batch_id": "setup",
            "commands": [
                {"cmd": "appoint", "appointment_id": "R1", "account": "acct-a",
                 "device_fp": "fp-race", "deadline": 1000},
                {"cmd": "appoint", "appointment_id": "R2", "account": "acct-b",
                 "device_fp": "fp-race", "deadline": 1000},
            ],
        }
        self.engine.apply_batch(setup)
        for appt_id in ("R1", "R2"):
            self.engine.apply_batch({
                "batch_id": f"attest-{appt_id}",
                "commands": [
                    {"cmd": "approve", "appointment_id": appt_id,
                     "witness": w, "content": "attest"}
                    for w in ("alice", "bob", "carol")
                ],
            })
        self.storage.close()

        b1 = self.tmp / "b1.json"
        b2 = self.tmp / "b2.json"
        write_json(b1, _fulfill_only_batch("race-1", "R1"))
        write_json(b2, _fulfill_only_batch("race-2", "R2"))

        env_cmd = [sys.executable, "-I", str(WORKER), str(self.db_path)]
        proc1 = subprocess.Popen(
            env_cmd + [str(b1)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        proc2 = subprocess.Popen(
            env_cmd + [str(b2)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        out1, err1 = proc1.communicate(timeout=60)
        out2, err2 = proc2.communicate(timeout=60)
        self.assertEqual(proc1.returncode, 0, err1.decode("utf-8", "replace"))
        self.assertEqual(proc2.returncode, 0, err2.decode("utf-8"))

        r1 = json.loads(out1.decode("utf-8"))
        r2 = json.loads(out2.decode("utf-8"))
        reasons = {
            r1["batch_id"]: [x["reason"] for x in r1["receipts"]],
            r2["batch_id"]: [x["reason"] for x in r2["receipts"]],
        }
        fulfill_reasons = [rs[-1] for rs in reasons.values()]
        # 恰好一个 OK，另一个 DEVICE_OCCUPIED
        self.assertEqual(sorted(fulfill_reasons), ["DEVICE_OCCUPIED", "OK"])
        # 两个批次都完整发布（各 1 条回执，无部分记录）
        for rs in reasons.values():
            self.assertEqual(len(rs), 1)

        # 重新打开核验：设备只有一个当前账户，事件链完整
        check = Storage(str(self.db_path))
        try:
            current = [
                r for r in check.list_registrations() if r["status"] == "current"
            ]
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["device_fp"], "fp-race")
            self.assertIn(current[0]["account"], ("acct-a", "acct-b"))
            ok, report = verify(check)
            self.assertTrue(ok, report["discrepancies"])
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
