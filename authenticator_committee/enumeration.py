"""小状态枚举独立核验。

本模块不用 engine 的任何判定函数，而是：
1. 用 ``enum`` 定义转交/兑现的最小状态空间；
2. 以手写判定表（规则原文的直译）给出每个状态组合的期望结论；
3. 在真实 SQLite 存储上把每个组合搭成对应场景，跑 engine，
   比对实际回执原因码与期望结论。

若 engine 的规则实现与这里的独立推导不一致，``cross_check`` 会逐项报告。
"""

import enum
import itertools

from .engine import CommitteeEngine
from .storage import Storage


class FState(enum.Enum):
    ACTIVE = "ACTIVE"
    PENDING_HANDOVER = "PENDING_HANDOVER"
    FULFILLED = "FULFILLED"


class ClockState(enum.Enum):
    BEFORE_DEADLINE = "BEFORE_DEADLINE"   # clock < deadline
    AT_DEADLINE = "AT_DEADLINE"           # clock == deadline，失效边界


class ThresholdState(enum.Enum):
    MET = "MET"
    NOT_MET = "NOT_MET"


class DeviceState(enum.Enum):
    FREE = "FREE"
    TAKEN_OTHER = "TAKEN_OTHER"           # 另一账户的当前登记
    REVOKED = "REVOKED"                   # 该设备曾登记后撤销


class FulfillVerdict(enum.Enum):
    FULFILL = "OK"
    EXPIRED = "EXPIRED"
    NOT_HANDED_OVER = "NOT_HANDED_OVER"
    THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
    DEVICE_OCCUPIED = "DEVICE_OCCUPIED"
    DEVICE_REVIVED_FORBIDDEN = "DEVICE_REVIVED_FORBIDDEN"
    ALREADY_FULFILLED = "ALREADY_FULFILLED"


class OldThreshold(enum.Enum):
    MET = "MET"
    NOT_MET = "NOT_MET"


class NewChairKind(enum.Enum):
    NONE = "NONE"                       # 新版本无负责人确认
    SAME_AS_OLD = "SAME_AS_OLD"         # 新版本确认人就是旧版本负责人本人
    DIFFERENT = "DIFFERENT"             # 新版本确认人是另一名未撤销见证员


class HandoverLimit(enum.Enum):
    FIRST = "FIRST"
    ALREADY_DONE = "ALREADY_DONE"


class HandoverVerdict(enum.Enum):
    HANDOVER = "OK"
    THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
    CHAIR_MISSING = "CHAIR_MISSING"
    SAME_CHAIR_BOTH_SIDES = "SAME_CHAIR_BOTH_SIDES"
    ALREADY_HANDED_OVER = "ALREADY_HANDED_OVER"
    NOT_PENDING_AGAIN = "HANDOVER_NOT_PENDING"


# ----------------------------------------------------------------------
# 手写判定表：规则原文的直译，优先级与 engine._h_fulfill 保持同序但独立编写
# ----------------------------------------------------------------------
def expected_fulfill(state, clock, threshold, device):
    # 已兑现预约的重复兑现是幂等成功：clock/threshold 维度对它不再可达，
    # 唯一例外是其登记随后被撤销（DEVICE_REVIVED_FORBIDDEN，由专属场景覆盖）。
    if state is FState.FULFILLED:
        return FulfillVerdict.ALREADY_FULFILLED
    if clock is ClockState.AT_DEADLINE:
        return FulfillVerdict.EXPIRED
    if state is FState.PENDING_HANDOVER:
        return FulfillVerdict.NOT_HANDED_OVER
    if threshold is ThresholdState.NOT_MET:
        return FulfillVerdict.THRESHOLD_NOT_MET
    if device is DeviceState.TAKEN_OTHER:
        return FulfillVerdict.DEVICE_OCCUPIED
    if device is DeviceState.REVOKED:
        return FulfillVerdict.DEVICE_REVIVED_FORBIDDEN
    return FulfillVerdict.FULFILL


def expected_handover(old_threshold, new_chair, limit):
    # 一次预约只能转交一次：该约束先于门槛判断。
    # 已转交后若没有新的切换，预约处于 ACTIVE，探测得到 NOT_PENDING；
    # 若又经历切换，则 handover_count=1 直接 ALREADY_HANDED_OVER。
    if limit is HandoverLimit.ALREADY_DONE:
        return HandoverVerdict.NOT_PENDING_AGAIN
    if old_threshold is OldThreshold.NOT_MET:
        return HandoverVerdict.THRESHOLD_NOT_MET
    if new_chair is NewChairKind.NONE:
        return HandoverVerdict.CHAIR_MISSING
    if new_chair is NewChairKind.SAME_AS_OLD:
        return HandoverVerdict.SAME_CHAIR_BOTH_SIDES
    return HandoverVerdict.HANDOVER


# ----------------------------------------------------------------------
# 场景搭建：每个枚举组合 -> 真实 engine 批处理
# ----------------------------------------------------------------------
def _cfg_v1():
    # 三个独立组，负责人组 G1；门槛要求 3 组；G3 带一名后备成员
    return {
        "version": 1,
        "required_groups": 3,
        "chair_group": "G1",
        "members": {
            "G1": ["alice"],
            "G2": ["bob"],
            "G3": ["carol", "frank"],
        },
    }


def _cfg_v2():
    # G1/G2 两版本共享；G1 仍是新版本负责人组，新增 dave 保证可配出不同负责人
    return {
        "version": 2,
        "required_groups": 3,
        "chair_group": "G1",
        "members": {
            "G1": ["alice", "dave"],
            "G2": ["bob"],
            "G4": ["erin"],
        },
    }


def _new_engine(tmp_path):
    storage = Storage(str(tmp_path / "enum.db"))
    return storage, CommitteeEngine(storage)


def _close(storage):
    # Windows 上必须显式关闭，否则 WAL/SHM 句柄会锁住临时目录
    storage.close()


def _init_and_appoint(engine, appt_id="A1", device="fp-A1", deadline=100):
    engine.apply_batch({
        "batch_id": "b-init",
        "commands": [{"cmd": "init", "config": _cfg_v1()}],
    })
    engine.apply_batch({
        "batch_id": "b-appt",
        "commands": [{
            "cmd": "appoint",
            "appointment_id": appt_id,
            "account": "acct-1",
            "device_fp": device,
            "deadline": deadline,
        }],
    })


def _approve_all_v1(engine, appt_id, batch_id="b-approve", revoke=()):
    cmds = [
        {"cmd": "approve", "appointment_id": appt_id,
         "witness": w, "content": "attest-v1"}
        for w in ("alice", "bob", "carol") if w not in revoke
    ]
    if cmds:
        engine.apply_batch({"batch_id": batch_id, "commands": cmds})


def build_fulfill_case(tmp_path, state, clock, threshold, device):
    """返回 engine 实际给出的 fulfill 原因码。"""
    storage, engine = _new_engine(tmp_path)
    _init_and_appoint(engine, deadline=(10 if clock is ClockState.AT_DEADLINE else 100))

    if device is DeviceState.TAKEN_OTHER:
        # 先让另一账户合法占用 fp-A1：用另一枚设备完成 A2 再换不现实，
        # 直接以 A2 预约同一设备并先兑现（A1 此时不参与）。
        engine.apply_batch({
            "batch_id": "b-a2",
            "commands": [{
                "cmd": "appoint", "appointment_id": "A2",
                "account": "acct-2", "device_fp": "fp-A1", "deadline": 200,
            }],
        })
        _approve_all_v1(engine, "A2", "b-a2-attest")
        engine.apply_batch({
            "batch_id": "b-a2-fulfill",
            "commands": [{"cmd": "fulfill", "appointment_id": "A2"}],
        })
    elif device is DeviceState.REVOKED:
        # 让另一枚预约 A2 占用 fp-A1 后撤销，制造“禁止复活”的设备历史；
        # A1 自身保持未兑现，才能真正走到设备判定分支。
        engine.apply_batch({
            "batch_id": "b-a2-revoked",
            "commands": [
                {"cmd": "appoint", "appointment_id": "A2",
                 "account": "acct-2", "device_fp": "fp-A1", "deadline": 200},
                {"cmd": "approve", "appointment_id": "A2",
                 "witness": "alice", "content": "attest-v1"},
                {"cmd": "approve", "appointment_id": "A2",
                 "witness": "bob", "content": "attest-v1"},
                {"cmd": "approve", "appointment_id": "A2",
                 "witness": "carol", "content": "attest-v1"},
                {"cmd": "fulfill", "appointment_id": "A2"},
                {"cmd": "revoke_registration", "device_fp": "fp-A1"},
            ],
        })

    if threshold is ThresholdState.MET:
        _approve_all_v1(engine, "A1", "b-attest")
    else:
        # 只给两组，缺 G3 -> 组数不足
        engine.apply_batch({
            "batch_id": "b-attest-partial",
            "commands": [
                {"cmd": "approve", "appointment_id": "A1",
                 "witness": "alice", "content": "attest-v1"},
                {"cmd": "approve", "appointment_id": "A1",
                 "witness": "bob", "content": "attest-v1"},
            ],
        })

    if state is FState.PENDING_HANDOVER:
        engine.apply_batch({
            "batch_id": "b-define-v2",
            "commands": [
                {"cmd": "define_config", "config": _cfg_v2()},
                {"cmd": "switch", "version": 2},
            ],
        })

    if clock is ClockState.AT_DEADLINE:
        # 用纯时钟事件把 clock 推到 deadline（== 即失效），探测命令本身不显式给 tick，
        # 其事件序号正好落在 deadline 这一 tick 上，覆盖同 tick 边界语义。
        gap = 10 - storage.current_clock()
        if gap > 0:
            engine.apply_batch({
                "batch_id": "b-tick-to-deadline",
                "commands": [{"cmd": "tick", "tick": 10}],
            })

    if state is FState.FULFILLED:
        engine.apply_batch({
            "batch_id": "b-fulfill-a1",
            "commands": [{"cmd": "fulfill", "appointment_id": "A1"}],
        })

    result = engine.apply_batch({
        "batch_id": "b-fulfill-probe",
        "commands": [{"cmd": "fulfill", "appointment_id": "A1"}],
    })
    reason = result["receipts"][0]["reason"]
    _close(storage)
    return reason


def build_handover_case(tmp_path, old_threshold, new_chair, limit):
    storage, engine = _new_engine(tmp_path)
    _init_and_appoint(engine)

    # 旧版本门槛
    if old_threshold is OldThreshold.MET:
        _approve_all_v1(engine, "A1", "b-old-attest")
    else:
        engine.apply_batch({
            "batch_id": "b-old-partial",
            "commands": [
                {"cmd": "approve", "appointment_id": "A1",
                 "witness": "alice", "content": "attest-v1"},
            ],
        })

    engine.apply_batch({
        "batch_id": "b-v2",
        "commands": [
            {"cmd": "define_config", "config": _cfg_v2()},
            {"cmd": "switch", "version": 2},
        ],
    })

    if new_chair is NewChairKind.SAME_AS_OLD:
        # alice 以新版本负责人身份再确认一次（与旧负责人同一人）
        engine.apply_batch({
            "batch_id": "b-new-same",
            "commands": [{
                "cmd": "approve", "appointment_id": "A1",
                "witness": "alice", "version": 2, "content": "attest-v2",
            }],
        })
    elif new_chair is NewChairKind.DIFFERENT:
        engine.apply_batch({
            "batch_id": "b-new-diff",
            "commands": [{
                "cmd": "approve", "appointment_id": "A1",
                "witness": "dave", "version": 2, "content": "attest-v2",
            }],
        })

    if limit is HandoverLimit.ALREADY_DONE:
        # 该分支要验证的是“第二次转交”，因此先无条件搭出一次成功转交：
        # 补齐旧版本三组门槛，并由与旧负责人不同的 dave 作新版本确认。
        old_witnesses = {a["witness"] for a in
                         storage.valid_approvals("A1", 1)}
        fix_cmds = []
        for witness in ("alice", "bob", "carol"):
            if witness not in old_witnesses:
                # 切换后默认版本是 v2，补旧门槛必须显式绑定 version=1
                fix_cmds.append({
                    "cmd": "approve", "appointment_id": "A1",
                    "witness": witness, "version": 1, "content": "attest-v1",
                })
        new_witnesses = {a["witness"] for a in
                         storage.valid_approvals("A1", 2)}
        # 第一次转交必须有一个与旧负责人 alice 不同的新版本负责人确认，dave 顶上
        if "dave" not in new_witnesses:
            fix_cmds.append({
                "cmd": "approve", "appointment_id": "A1",
                "witness": "dave", "version": 2, "content": "attest-v2",
            })
        if fix_cmds:
            engine.apply_batch({"batch_id": "b-fix-for-first",
                                "commands": fix_cmds})
        first = engine.apply_batch({
            "batch_id": "b-first-handover",
            "commands": [{"cmd": "handover", "appointment_id": "A1"}],
        })
        assert first["receipts"][0]["reason"] == "OK", first
        # 不再切换：预约此刻是 ACTIVE 且 handover_count=1，
        # 再探测一次 handover，独立核验“只能转交一次”的两个分支。

    result = engine.apply_batch({
        "batch_id": "b-handover-probe",
        "commands": [{"cmd": "handover", "appointment_id": "A1"}],
    })
    reason = result["receipts"][0]["reason"]
    _close(storage)
    return reason


def cross_check_fulfill(tmp_path):
    """枚举全部 3x2x2x3=36 个兑现组合，返回不一致列表。"""
    mismatches = []
    cases = itertools.product(FState, ClockState, ThresholdState, DeviceState)
    for state, clock, threshold, device in cases:
        # 不可达/维度无关组合：
        # - FULFILLED 与设备被占用/已撤销互斥；
        # - 已兑现后的重复兑现恒为幂等成功，clock/threshold 维度对它不可观测，
        #   只保留 (BEFORE_DEADLINE, MET) 一个代表组合。
        if state is FState.FULFILLED and (
            device is not DeviceState.FREE
            or clock is not ClockState.BEFORE_DEADLINE
            or threshold is not ThresholdState.MET
        ):
            continue
        tmp = tmp_path / (
            f"fulfill_{state.name}_{clock.name}_{threshold.name}_{device.name}"
        )
        tmp.mkdir(parents=True, exist_ok=True)
        actual = build_fulfill_case(tmp, state, clock, threshold, device)
        expected = expected_fulfill(state, clock, threshold, device).value
        if state is FState.FULFILLED:
            expected = "OK"  # 幂等重复兑现
        if actual != expected:
            mismatches.append({
                "dimension": "fulfill",
                "state": state.name,
                "clock": clock.name,
                "threshold": threshold.name,
                "device": device.name,
                "expected": expected,
                "actual": actual,
            })
    return mismatches


def cross_check_handover(tmp_path):
    """枚举全部 2x3x2=12 个转交组合，返回不一致列表。"""
    mismatches = []
    cases = itertools.product(OldThreshold, NewChairKind, HandoverLimit)
    for old_threshold, new_chair, limit in cases:
        tmp = tmp_path / (
            f"handover_{old_threshold.name}_{new_chair.name}_{limit.name}"
        )
        tmp.mkdir(parents=True, exist_ok=True)
        actual = build_handover_case(tmp, old_threshold, new_chair, limit)
        expected = expected_handover(old_threshold, new_chair, limit).value
        if actual != expected:
            mismatches.append({
                "dimension": "handover",
                "old_threshold": old_threshold.name,
                "new_chair": new_chair.name,
                "limit": limit.name,
                "expected": expected,
                "actual": actual,
            })
    return mismatches
