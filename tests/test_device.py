"""设备指纹唯一性：双账户争用、撤销后禁止复活、全局单一当前账户。"""

import unittest

from helpers import BaseTestCase
from authenticator_committee.projection import verify


class TestDeviceUniqueness(BaseTestCase):

    def _two_appointments_same_device(self):
        self.init_v1()
        self.appoint("A1", account="acct-1", device="fp-shared")
        self.apply([{
            "cmd": "appoint", "appointment_id": "A2", "account": "acct-2",
            "device_fp": "fp-shared", "deadline": 100,
        }])
        self.approve_v1_full("A1")
        self.approve_v1_full("A2")

    def test_first_fulfill_wins_device(self):
        self._two_appointments_same_device()
        r1 = self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(r1), "OK")
        r2 = self.apply([{"cmd": "fulfill", "appointment_id": "A2"}])
        self.assertEqual(self.first_reason(r2), "DEVICE_OCCUPIED")
        # 全局只有一个当前账户
        current = {
            r["device_fp"]: r["account"]
            for r in self.storage.list_registrations() if r["status"] == "current"
        }
        self.assertEqual(current, {"fp-shared": "acct-1"})

    def test_appoint_toward_occupied_device_rejected(self):
        self._two_appointments_same_device()
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        r = self.apply([{
            "cmd": "appoint", "appointment_id": "A3", "account": "acct-3",
            "device_fp": "fp-shared", "deadline": 100,
        }])
        self.assertEqual(self.first_reason(r), "DEVICE_OCCUPIED")

    def test_revoked_registration_cannot_be_revived(self):
        self.init_v1()
        self.appoint("A1", device="fp-x")
        self.approve_v1_full("A1")
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.apply([{"cmd": "revoke_registration", "device_fp": "fp-x"}])
        # 撤销后设备空闲，但同指纹新预约禁止复活
        r = self.apply([{
            "cmd": "appoint", "appointment_id": "A2", "account": "acct-2",
            "device_fp": "fp-x", "deadline": 100,
        }])
        self.assertEqual(self.first_reason(r), "DEVICE_REVIVED_FORBIDDEN")
        # 原登记保留为历史
        regs = self.storage.list_registrations()
        self.assertEqual([r["status"] for r in regs], ["revoked"])

    def test_revoke_registration_guards(self):
        r = self.apply([{"cmd": "revoke_registration", "device_fp": "ghost"}])
        self.assertEqual(self.first_reason(r), "REGISTRATION_NOT_FOUND")
        self.init_v1()
        self.appoint("A1", device="fp-y")
        self.approve_v1_full("A1")
        self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])
        self.assertEqual(self.first_reason(
            self.apply([{"cmd": "revoke_registration", "device_fp": "fp-y"}])),
            "OK")
        # 重复撤销是业务拒绝
        self.assertEqual(self.first_reason(
            self.apply([{"cmd": "revoke_registration", "device_fp": "fp-y"}])),
            "REGISTRATION_REVOKED")

    def test_distinct_devices_coexist(self):
        self.init_v1()
        self.appoint("A1", device="fp-1")
        self.apply([{
            "cmd": "appoint", "appointment_id": "A2", "account": "acct-2",
            "device_fp": "fp-2", "deadline": 100,
        }])
        self.approve_v1_full("A1")
        self.approve_v1_full("A2")
        self.assertEqual(self.first_reason(
            self.apply([{"cmd": "fulfill", "appointment_id": "A1"}])), "OK")
        self.assertEqual(self.first_reason(
            self.apply([{"cmd": "fulfill", "appointment_id": "A2"}])), "OK")
        ok, report = verify(self.storage)
        self.assertTrue(ok, report["discrepancies"])


if __name__ == "__main__":
    unittest.main()
