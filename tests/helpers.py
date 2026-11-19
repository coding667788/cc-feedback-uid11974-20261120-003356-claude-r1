"""测试公共工具：临时存储、标准三版本配置、常用批次构造。"""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authenticator_committee.engine import CommitteeEngine
from authenticator_committee.storage import Storage

# ---- 虚构测试配置 ---------------------------------------------------------
# v1：三个独立组，负责人组 G1，门槛 3 组。G3 配后备成员 frank，
# 用于演示“见证员撤销后由同组后备补位”的撤销/兑现交错场景。
CFG_V1 = {
    "version": 1,
    "required_groups": 3,
    "chair_group": "G1",
    "members": {
        "G1": ["alice"],
        "G2": ["bob"],
        "G3": ["carol", "frank"],
    },
}

# v2：G1/G2 与 v1 共享（跨组人员变更场景）；G1 仍为负责人组，新增 dave
# 以便“新旧负责人不能同一人”时能配出不同的人；G3 被移除、新增 G4/erin
CFG_V2 = {
    "version": 2,
    "required_groups": 3,
    "chair_group": "G1",
    "members": {
        "G1": ["alice", "dave"],
        "G2": ["bob"],
        "G4": ["erin"],
    },
}

# v3：单组配置，用于多次切换与二次转交
CFG_V3 = {
    "version": 3,
    "required_groups": 1,
    "chair_group": "G1",
    "members": {"G1": ["alice", "dave"]},
}


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.db_path = self.tmp / "committee.db"
        self.storage = Storage(str(self.db_path))
        self.engine = CommitteeEngine(self.storage)
        self._batch_seq = 0

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def bid(self, prefix="b"):
        self._batch_seq += 1
        return f"{prefix}-{self._batch_seq}"

    def apply(self, commands, batch_id=None):
        return self.engine.apply_batch({
            "batch_id": batch_id or self.bid(),
            "commands": commands,
        })

    def init_v1(self):
        return self.apply([{"cmd": "init", "config": CFG_V1}], "b-init")

    def define_switch_v2(self):
        return self.apply([
            {"cmd": "define_config", "config": CFG_V2},
            {"cmd": "switch", "version": 2},
        ], "b-v2")

    def appoint(self, appt_id="A1", account="acct-1", device="fp-1",
                deadline=100):
        return self.apply([{
            "cmd": "appoint",
            "appointment_id": appt_id,
            "account": account,
            "device_fp": device,
            "deadline": deadline,
        }])

    def approve(self, appt_id, witness, content="attest", version=None):
        cmd = {
            "cmd": "approve",
            "appointment_id": appt_id,
            "witness": witness,
            "content": content,
        }
        if version is not None:
            cmd["version"] = version
        return self.apply([cmd])

    def approve_v1_full(self, appt_id):
        return self.apply([
            {"cmd": "approve", "appointment_id": appt_id,
             "witness": w, "content": "attest-v1"}
            for w in ("alice", "bob", "carol")
        ])

    def reasons(self, result):
        return [r["reason"] for r in result["receipts"]]

    def first_reason(self, result):
        return result["receipts"][0]["reason"]

    def reload_storage(self):
        """模拟重启：关掉连接再用同一数据库文件打开。"""
        self.storage.close()
        self.storage = Storage(str(self.db_path))
        self.engine = CommitteeEngine(self.storage)


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
