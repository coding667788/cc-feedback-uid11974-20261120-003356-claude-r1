"""CLI 端到端测试与小状态枚举独立核验。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import BaseTestCase, write_json
from authenticator_committee import enumeration as enum_mod

CLI = Path(__file__).resolve().parents[1] / "authenticator_committee" / "cli.py"


def run_cli(db_path, *args):
    # --db 属于各子命令，必须放在子命令名之后
    command, rest = args[0], args[1:]
    proc = subprocess.run(
        [sys.executable, "-I", str(CLI), command, "--db", db_path, *rest],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    return proc


class TestCli(BaseTestCase):

    def test_full_cli_lifecycle(self):
        db = str(self.db_path)

        # init（默认配置）
        proc = run_cli(db, "init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        init_result = json.loads(proc.stdout)
        self.assertTrue(init_result["published"])

        # apply 一个完整批次
        batch = {
            "batch_id": "cli-batch",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A1",
                 "account": "acct-1", "device_fp": "fp-cli", "deadline": 50},
                {"cmd": "approve", "appointment_id": "A1",
                 "witness": "alice", "content": "ok"},
                {"cmd": "approve", "appointment_id": "A1",
                 "witness": "bob", "content": "ok"},
                {"cmd": "fulfill", "appointment_id": "A1"},
            ],
        }
        batch_path = self.tmp / "batch.json"
        write_json(batch_path, batch)
        proc = run_cli(db, "apply", "--input", str(batch_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        applied = json.loads(proc.stdout)
        self.assertEqual(applied["summary"]["accepted"], 4)

        # inspect
        proc = run_cli(db, "inspect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        view = json.loads(proc.stdout)
        self.assertEqual(view["current_devices"], {"fp-cli": "acct-1"})
        self.assertEqual(view["appointments"][0]["status"], "FULFILLED")
        self.assertTrue(any(
            e["stage"] == "fulfilled" for e in view["evidence"]
        ))

        # verify
        proc = run_cli(db, "verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(report["hash_chain_ok"])

    def test_invalid_batch_exit_code_and_rollback(self):
        db = str(self.db_path)
        run_cli(db, "init")
        bad = {"batch_id": "bad", "commands": [{"cmd": "appoint"}]}  # 缺字段
        bad_path = self.tmp / "bad.json"
        write_json(bad_path, bad)
        proc = run_cli(db, "apply", "--input", str(bad_path))
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertTrue(err["rolled_back"])

    def test_verify_detects_tampering_exit_code_3(self):
        db = str(self.db_path)
        run_cli(db, "init")
        self.storage.close()
        # 外部直接改库
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE events SET event_hash = 'deadbeef' WHERE seq = 1")
        conn.commit()
        conn.close()
        proc = run_cli(db, "verify")
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["ok"])


class TestEnumerationCrossCheck(BaseTestCase):

    def test_fulfill_truth_table(self):
        mismatches = enum_mod.cross_check_fulfill(self.tmp / "enum-f")
        self.assertEqual(mismatches, [])

    def test_handover_truth_table(self):
        mismatches = enum_mod.cross_check_handover(self.tmp / "enum-h")
        self.assertEqual(mismatches, [])

    def test_expected_tables_are_exhaustive_and_stable(self):
        # 守护判定表自身：兑现优先级链的顺序契约
        from authenticator_committee.enumeration import (
            ClockState, DeviceState, FState, FulfillVerdict,
            ThresholdState, expected_fulfill,
        )
        self.assertEqual(
            expected_fulfill(FState.ACTIVE, ClockState.AT_DEADLINE,
                             ThresholdState.MET, DeviceState.TAKEN_OTHER),
            FulfillVerdict.EXPIRED,
        )
        self.assertEqual(
            expected_fulfill(FState.PENDING_HANDOVER, ClockState.BEFORE_DEADLINE,
                             ThresholdState.MET, DeviceState.FREE),
            FulfillVerdict.NOT_HANDED_OVER,
        )
        self.assertEqual(
            expected_fulfill(FState.ACTIVE, ClockState.BEFORE_DEADLINE,
                             ThresholdState.NOT_MET, DeviceState.TAKEN_OTHER),
            FulfillVerdict.THRESHOLD_NOT_MET,
        )
        self.assertEqual(
            expected_fulfill(FState.ACTIVE, ClockState.BEFORE_DEADLINE,
                             ThresholdState.MET, DeviceState.TAKEN_OTHER),
            FulfillVerdict.DEVICE_OCCUPIED,
        )
        self.assertEqual(
            expected_fulfill(FState.ACTIVE, ClockState.BEFORE_DEADLINE,
                             ThresholdState.MET, DeviceState.REVOKED),
            FulfillVerdict.DEVICE_REVIVED_FORBIDDEN,
        )


if __name__ == "__main__":
    unittest.main()
