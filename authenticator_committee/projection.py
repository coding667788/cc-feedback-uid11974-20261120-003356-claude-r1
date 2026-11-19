"""事件重放与交叉核验。

``replay`` 只读取 events 表，用一份与 engine 物化写入相互独立的内存模型
重新推导出全部可见状态；``verify`` 再把重放结果与物化表逐字段比对，
并复核哈希链与时钟单调性，保证重启后状态可由原始事件完整重建。
"""

import json

from .storage import GENESIS_HASH, compute_event_hash, canonical_json


class ReplayState:
    """纯事件重放得到的状态，刻意不依赖任何物化表。"""

    def __init__(self):
        self.clock = 0
        self.configs = {}                 # version -> 配置对象
        self.active_version = None
        self.appointments = {}            # id -> 预约
        self.approvals = {}               # (appt, witness, version) -> 审批
        self.revocations = {}             # witness -> 撤销事件序号
        self.registrations = []           # 登记列表（含历史）
        self.current_devices = {}         # device_fp -> account
        self.ticks = []                   # (seq, tick)
        self.chain_ok = True
        self.chain_break = None
        self.event_types = []

    def to_summary(self):
        return {
            "clock": self.clock,
            "active_version": self.active_version,
            "config_versions": sorted(self.configs),
            "appointments": {
                k: {
                    "account": v["account"],
                    "device_fp": v["device_fp"],
                    "deadline": v["deadline"],
                    "version": v["version"],
                    "status": v["status"],
                    "handover_count": v["handover_count"],
                }
                for k, v in sorted(self.appointments.items())
            },
            "current_devices": dict(sorted(self.current_devices.items())),
            "registrations": [
                {
                    "appointment_id": r["appointment_id"],
                    "account": r["account"],
                    "device_fp": r["device_fp"],
                    "version": r["version"],
                    "status": r["status"],
                }
                for r in self.registrations
            ],
            "revoked_witnesses": sorted(self.revocations),
        }


def replay(storage):
    state = ReplayState()
    prev_hash = GENESIS_HASH
    for event in storage.all_events():
        payload = json.loads(event["payload"])
        expect = compute_event_hash(
            prev_hash, event["event_type"], event["tick"], payload
        )
        if expect != event["event_hash"]:
            state.chain_ok = False
            state.chain_break = event["seq"]
            break
        if state.ticks and event["tick"] < state.ticks[-1][1]:
            state.chain_ok = False
            state.chain_break = event["seq"]
            break
        prev_hash = event["event_hash"]
        state.clock = event["tick"]
        state.ticks.append((event["seq"], event["tick"]))
        state.event_types.append(event["event_type"])
        _apply_event(state, event["seq"], event["event_type"], payload)
    return state


def _membership(config, witness):
    return next(
        (g for g, roster in config["members"].items() if witness in roster),
        None,
    )


def _valid_approval_groups(state, appointment_id, version):
    """重放视角下的有效见证：未取代、见证员未撤销，按组去重。"""
    groups = {}
    for (appt_id, witness, ver), approval in state.approvals.items():
        if appt_id != appointment_id or ver != version:
            continue
        if approval["superseded"] or witness in state.revocations:
            continue
        groups.setdefault(approval["group_name"], set()).add(witness)
    return groups


def _threshold_met(state, appointment_id, version):
    config = state.configs[version]
    groups = _valid_approval_groups(state, appointment_id, version)
    return (
        len(groups) >= config["required_groups"]
        and config["chair_group"] in groups
    )


def _apply_event(state, seq, event_type, p):
    if event_type == "committee_initialized":
        cfg = p["config"]
        state.configs[cfg["version"]] = cfg
        state.active_version = cfg["version"]

    elif event_type == "config_defined":
        cfg = p["config"]
        state.configs[cfg["version"]] = cfg

    elif event_type == "config_switched":
        target = p["version"]
        state.active_version = target
        for appt in state.appointments.values():
            if appt["status"] != "FULFILLED":
                appt["status"] = "PENDING_HANDOVER"

    elif event_type == "appointment_created":
        state.appointments[p["appointment_id"]] = {
            "account": p["account"],
            "device_fp": p["device_fp"],
            "deadline": p["deadline"],
            "version": p["version"],
            "status": "ACTIVE",
            "handover_count": 0,
        }

    elif event_type == "approval_recorded":
        state.approvals[
            (p["appointment_id"], p["witness"], p["version"])
        ] = {
            "group_name": p["group_name"],
            "content": p["content"],
            "superseded": False,
        }

    elif event_type == "witness_revoked":
        state.revocations[p["witness"]] = seq

    elif event_type == "committee_handed_over":
        appt = state.appointments[p["appointment_id"]]
        old_version = p["from_version"]
        for (appt_id, _witness, ver), approval in state.approvals.items():
            if appt_id == p["appointment_id"] and ver == old_version:
                approval["superseded"] = True
        appt["version"] = p["to_version"]
        appt["status"] = "ACTIVE"
        appt["handover_count"] += 1

    elif event_type == "authenticator_fulfilled":
        appt = state.appointments[p["appointment_id"]]
        appt["status"] = "FULFILLED"
        state.registrations.append({
            "appointment_id": p["appointment_id"],
            "account": p["account"],
            "device_fp": p["device_fp"],
            "version": p["version"],
            "status": "current",
        })
        state.current_devices[p["device_fp"]] = p["account"]

    elif event_type == "registration_revoked":
        for reg in state.registrations:
            if reg["device_fp"] == p["device_fp"] and reg["status"] == "current":
                reg["status"] = "revoked"
        state.current_devices.pop(p["device_fp"], None)


def verify(storage):
    """返回 (ok, 报告字典)。任何不一致都列出具体差异。"""
    discrepancies = []
    state = replay(storage)

    if not state.chain_ok:
        discrepancies.append(f"哈希链/时钟顺序在事件 {state.chain_break} 处断裂")

    # 时钟
    if state.clock != storage.current_clock():
        discrepancies.append(
            f"时钟不一致：重放 {state.clock} / 物化 {storage.current_clock()}"
        )

    # 生效版本
    if state.active_version != storage.active_version():
        discrepancies.append(
            f"生效版本不一致：重放 {state.active_version} / "
            f"物化 {storage.active_version()}"
        )

    # 配置
    stored_configs = {c["version"]: c for c in storage.list_configs()}
    if set(state.configs) != set(stored_configs):
        discrepancies.append("配置版本集合不一致")
    for version, cfg in state.configs.items():
        stored = stored_configs.get(version)
        if stored is None:
            continue
        if stored["required_groups"] != cfg["required_groups"] \
                or stored["chair_group"] != cfg["chair_group"] \
                or canonical_json(stored["members"]) != canonical_json(cfg["members"]):
            discrepancies.append(f"配置 v{version} 内容不一致")

    # 预约
    stored_appts = {a["appointment_id"]: a for a in storage.list_appointments()}
    if set(state.appointments) != set(stored_appts):
        discrepancies.append(
            f"预约集合不一致：重放 {sorted(state.appointments)} / "
            f"物化 {sorted(stored_appts)}"
        )
    for appt_id, appt in state.appointments.items():
        stored = stored_appts.get(appt_id)
        if stored is None:
            continue
        for field in ("account", "device_fp", "deadline", "version",
                      "status", "handover_count"):
            if appt[field] != stored[field]:
                discrepancies.append(
                    f"预约 {appt_id} 字段 {field} 不一致："
                    f"重放 {appt[field]} / 物化 {stored[field]}"
                )

    # 登记与设备占用
    stored_regs = storage.list_registrations()
    if len(stored_regs) != len(state.registrations):
        discrepancies.append("登记历史条数不一致")
    for replayed, stored in zip(state.registrations, stored_regs):
        for field in ("appointment_id", "account", "device_fp", "version", "status"):
            if replayed[field] != stored[field]:
                discrepancies.append(
                    f"登记 {stored['device_fp']} 字段 {field} 不一致"
                )
    current = {
        r["device_fp"]: r["account"]
        for r in stored_regs if r["status"] == "current"
    }
    if current != state.current_devices:
        discrepancies.append(
            f"当前设备占用不一致：重放 {state.current_devices} / 物化 {current}"
        )

    # 撤销名单
    stored_revoked = {r["witness"] for r in storage.list_revocations()}
    if stored_revoked != set(state.revocations):
        discrepancies.append("撤销见证员集合不一致")

    # 用重放模型独立复核每条已兑现登记在兑现时的门槛是否真实达标
    for reg in stored_regs:
        appt = state.appointments[reg["appointment_id"]]
        if not _threshold_met(state, reg["appointment_id"], appt["version"]):
            # 已取代/已撤销审批会让当前视角变化；仅当登记仍 current 时才报错
            if reg["status"] == "current":
                discrepancies.append(
                    f"登记 {reg['device_fp']} 重放复核门槛不达标"
                )

    report = {
        "ok": not discrepancies,
        "event_count": len(storage.all_events()),
        "clock": state.clock,
        "hash_chain_ok": state.chain_ok,
        "discrepancies": discrepancies,
        "replayed_summary": state.to_summary(),
    }
    return not discrepancies, report
