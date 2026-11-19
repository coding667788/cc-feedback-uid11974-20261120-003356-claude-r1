"""批处理引擎：输入校验、委员会业务规则、事件驱动逻辑时钟。

时钟规则
--------
每条被接受的命令追加一个事件，事件 tick 即逻辑时钟；命令可显式给 tick，
默认取当前时钟 +1，tick 倒退作为业务拒绝（TICK_OUT_OF_ORDER）。
预约在 ``clock >= deadline``（相等即失效）时不能再兑现。
同一 tick 内的先后顺序由 events.seq 唯一确定。

事务规则
--------
每个批次在一个 BEGIN IMMEDIATE 事务内处理：结构性输入错误抛 InvalidBatch，
整批回滚且不留任何痕迹；业务规则拒绝照常生成回执并随批次一起提交。
"""

from datetime import datetime, timezone
import sqlite3

from .errors import EngineFault, InvalidBatch, REASON_TEXT

KNOWN_COMMANDS = {
    "init",
    "define_config",
    "switch",
    "appoint",
    "approve",
    "revoke",
    "handover",
    "fulfill",
    "revoke_registration",
    "tick",
}

# 命令 -> 回执主引用字段
_REF_FIELD = {
    "init": None,
    "define_config": None,
    "switch": "version",
    "appoint": "appointment_id",
    "approve": "appointment_id",
    "revoke": "witness",
    "handover": "appointment_id",
    "fulfill": "appointment_id",
    "revoke_registration": "device_fp",
    "tick": None,
}


def _is_nonempty_str(value):
    return isinstance(value, str) and len(value) > 0


def _is_positive_int(value):
    # bool 是 int 的子类，显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_config(cfg, allow_any_version):
    if not isinstance(cfg, dict):
        raise InvalidBatch("config 必须是对象")
    if not _is_positive_int(cfg.get("version")):
        raise InvalidBatch("config.version 必须是正整数")
    if not allow_any_version and cfg["version"] != 1:
        raise InvalidBatch("init 的配置版本必须为 1")
    if not _is_positive_int(cfg.get("required_groups")):
        raise InvalidBatch("config.required_groups 必须是正整数")
    if not _is_nonempty_str(cfg.get("chair_group")):
        raise InvalidBatch("config.chair_group 必须是非空字符串")
    members = cfg.get("members")
    if not isinstance(members, dict) or not members:
        raise InvalidBatch("config.members 必须是非空对象：组名 -> 见证员列表")
    seen = set()
    for group_name, witnesses in members.items():
        if not _is_nonempty_str(group_name):
            raise InvalidBatch("组成名必须是非空字符串")
        if not isinstance(witnesses, list) or not witnesses:
            raise InvalidBatch(f"组 {group_name} 的见证员列表不能为空")
        for witness in witnesses:
            if not _is_nonempty_str(witness):
                raise InvalidBatch(f"组 {group_name} 存在非法见证员名")
            if witness in seen:
                raise InvalidBatch(f"见证员 {witness} 在同一配置中跨组重复")
            seen.add(witness)
    if cfg["chair_group"] not in members:
        raise InvalidBatch("负责人组必须存在于 members 中")
    if cfg["required_groups"] > len(members):
        raise InvalidBatch("所需组数不能超过配置中的实际组数")


def validate_batch(batch):
    """纯结构性校验。任何问题都抛 InvalidBatch，调用方不得写入任何数据。"""
    if not isinstance(batch, dict):
        raise InvalidBatch("批次必须是 JSON 对象")
    if not _is_nonempty_str(batch.get("batch_id")):
        raise InvalidBatch("batch_id 必须是非空字符串")
    commands = batch.get("commands")
    if not isinstance(commands, list) or not commands:
        raise InvalidBatch("commands 必须是非空数组")

    appointed_ids = set()
    for index, cmd in enumerate(commands):
        where = f"commands[{index}]"
        if not isinstance(cmd, dict):
            raise InvalidBatch(f"{where} 必须是对象")
        name = cmd.get("cmd")
        if name not in KNOWN_COMMANDS:
            raise InvalidBatch(f"{where}.cmd 未知：{name!r}")
        if "tick" in cmd and not _is_positive_int(cmd["tick"]):
            raise InvalidBatch(f"{where}.tick 必须是正整数")

        if name in ("init", "define_config"):
            _validate_config(cmd.get("config"), allow_any_version=(name == "define_config"))
        elif name == "switch":
            if not _is_positive_int(cmd.get("version")):
                raise InvalidBatch(f"{where}.version 必须是正整数")
        elif name == "appoint":
            for field in ("appointment_id", "account", "device_fp"):
                if not _is_nonempty_str(cmd.get(field)):
                    raise InvalidBatch(f"{where}.{field} 必须是非空字符串")
            if not _is_positive_int(cmd.get("deadline")):
                raise InvalidBatch(f"{where}.deadline 必须是正整数")
            if cmd["appointment_id"] in appointed_ids:
                raise InvalidBatch(f"{where} 在同一批次中重复预约 {cmd['appointment_id']}")
            appointed_ids.add(cmd["appointment_id"])
        elif name == "approve":
            for field in ("appointment_id", "witness", "content"):
                if not _is_nonempty_str(cmd.get(field)):
                    raise InvalidBatch(f"{where}.{field} 必须是非空字符串")
            if "version" in cmd and not _is_positive_int(cmd["version"]):
                raise InvalidBatch(f"{where}.version 必须是正整数")
        elif name == "revoke":
            if not _is_nonempty_str(cmd.get("witness")):
                raise InvalidBatch(f"{where}.witness 必须是非空字符串")
        elif name in ("handover", "fulfill"):
            if not _is_nonempty_str(cmd.get("appointment_id")):
                raise InvalidBatch(f"{where}.appointment_id 必须是非空字符串")
        elif name == "revoke_registration":
            if not _is_nonempty_str(cmd.get("device_fp")):
                raise InvalidBatch(f"{where}.device_fp 必须是非空字符串")
        # tick 为纯时钟推进命令，无额外字段


def evaluate_threshold(storage, appointment_id, version):
    """按组去重统计有效见证，返回 (是否达标, 覆盖组列表, 明细)。"""
    config = storage.get_config(version)
    approvals = storage.valid_approvals(appointment_id, version)
    groups = {}
    for approval in approvals:
        groups.setdefault(approval["group_name"], []).append(approval["witness"])
    covered = sorted(groups)
    chair_ok = config["chair_group"] in covered
    enough = len(covered) >= config["required_groups"]
    detail = {
        "version": version,
        "required_groups": config["required_groups"],
        "chair_group": config["chair_group"],
        "covered_groups": covered,
        "group_witnesses": {g: sorted(set(w)) for g, w in groups.items()},
        "chair_covered": chair_ok,
        "enough_groups": enough,
        "met": chair_ok and enough,
    }
    return chair_ok and enough, covered, detail


class CommitteeEngine:
    """每个实例绑定一个 Storage；方法内自行管理单批次事务。"""

    def __init__(self, storage):
        self.storage = storage

    # ------------------------------------------------------------------
    # 批次入口
    # ------------------------------------------------------------------
    def apply_batch(self, batch, fault=None):
        validate_batch(batch)  # 结构无效：根本不开启事务
        batch_id = batch["batch_id"]
        storage = self.storage

        storage.begin_immediate()
        self._batch_id = batch_id
        try:
            existing = storage.get_batch(batch_id)
            if existing is not None:
                # 幂等重放：原封不动返回已发布回执，不新增任何事件
                receipts = storage.list_receipts(batch_id=batch_id)
                storage.commit()
                return self._result(batch_id, receipts, replayed=True)

            first_seq = storage.last_event_seq() + 1
            receipts = []
            for index, cmd in enumerate(batch["commands"]):
                receipts.append(self._dispatch(batch_id, index, cmd))

            if fault == "before_commit":
                # 测试钩子：事件与回执都已写入但尚未提交，整体回滚
                raise EngineFault("注入故障：批次提交前中止")

            last_seq = storage.last_event_seq()
            storage.insert_batch(
                batch_id, first_seq, last_seq, len(batch["commands"])
            )
            storage.commit()
        except Exception:
            storage.rollback()
            raise

        final_receipts = storage.list_receipts(batch_id=batch_id)
        return self._result(batch_id, final_receipts, replayed=False)

    def _result(self, batch_id, receipts, replayed):
        accepted = sum(1 for r in receipts if r["accepted"])
        return {
            "batch_id": batch_id,
            "published": True,
            "replayed": replayed,
            "clock": self.storage.current_clock(),
            "summary": {
                "total": len(receipts),
                "accepted": accepted,
                "rejected": len(receipts) - accepted,
            },
            "receipts": [
                {
                    "index": r["command_index"],
                    "command": r["command"],
                    "ref_id": r["ref_id"],
                    "accepted": bool(r["accepted"]),
                    "reason": r["reason"],
                    "message": r["message"],
                    "event_seq": r["event_seq"],
                }
                for r in receipts
            ],
        }

    # ------------------------------------------------------------------
    # 分发与回执
    # ------------------------------------------------------------------
    def _dispatch(self, batch_id, index, cmd):
        name = cmd["cmd"]
        tick_reason = self._check_tick(cmd)
        if tick_reason is not None:
            reason = tick_reason
            event_seq = None
        else:
            handler = getattr(self, f"_h_{name}")
            reason, event_seq = handler(cmd)
        ref_field = _REF_FIELD[name]
        ref_id = cmd.get(ref_field) if ref_field else None
        self.storage.add_receipt(
            batch_id,
            index,
            name,
            ref_id,
            accepted=(reason == "OK"),
            reason=reason,
            message=REASON_TEXT[reason],
            event_seq=event_seq,
        )
        return reason

    def _check_tick(self, cmd):
        if "tick" not in cmd:
            return None
        if cmd["tick"] < self.storage.current_clock():
            return "TICK_OUT_OF_ORDER"
        return None

    def _tick(self, cmd):
        return cmd.get("tick", self.storage.current_clock() + 1)

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _append(self, event_type, cmd, payload):
        tick = self._tick(cmd)
        seq, _ = self.storage.append_event(
            self._now(), event_type, tick, self._batch_id, payload
        )
        return seq

    # ------------------------------------------------------------------
    # 命令处理：每个方法返回 (原因码, 事件序号或 None)
    # ------------------------------------------------------------------
    def _h_init(self, cmd):
        storage = self.storage
        if storage.active_version() is not None:
            return "ALREADY_INITIALIZED", None
        cfg = cmd["config"]
        seq = self._append("committee_initialized", cmd, {"config": cfg})
        storage.insert_config(
            cfg["version"], cfg["required_groups"], cfg["chair_group"],
            cfg["members"], seq,
        )
        storage.set_active_version(cfg["version"])
        return "OK", seq

    def _h_define_config(self, cmd):
        storage = self.storage
        if storage.active_version() is None:
            return "NOT_INITIALIZED", None
        cfg = cmd["config"]
        if storage.get_config(cfg["version"]) is not None:
            return "VERSION_ALREADY_DEFINED", None
        seq = self._append("config_defined", cmd, {"config": cfg})
        storage.insert_config(
            cfg["version"], cfg["required_groups"], cfg["chair_group"],
            cfg["members"], seq,
        )
        return "OK", seq

    def _h_switch(self, cmd):
        storage = self.storage
        if storage.active_version() is None:
            return "NOT_INITIALIZED", None
        target = cmd["version"]
        if storage.get_config(target) is None:
            return "VERSION_NOT_DEFINED", None
        if target == storage.active_version():
            return "SWITCH_SAME_VERSION", None
        seq = self._append("config_switched", cmd, {"version": target})
        storage.set_active_version(target)
        # 所有未兑现预约进入待转交（已经待转交的 retarget 到最新版本）
        for appt in storage.list_appointments():
            if appt["status"] == "FULFILLED":
                continue
            if appt["status"] != "PENDING_HANDOVER":
                storage.update_appointment(
                    appt["appointment_id"], status="PENDING_HANDOVER"
                )
            storage.add_evidence(
                appt["appointment_id"],
                "switched_pending",
                self.storage.current_clock(),
                [],
                {"from_version": appt["version"], "to_version": target},
                seq,
            )
        return "OK", seq

    def _h_appoint(self, cmd):
        storage = self.storage
        if storage.active_version() is None:
            return "NOT_INITIALIZED", None
        if storage.get_appointment(cmd["appointment_id"]) is not None:
            return "DUPLICATE_APPOINTMENT", None
        reg = storage.registration_for_device(cmd["device_fp"])
        if reg is not None:
            if reg["status"] == "current":
                return "DEVICE_OCCUPIED", None
            return "DEVICE_REVIVED_FORBIDDEN", None
        version = storage.active_version()
        payload = {
            "appointment_id": cmd["appointment_id"],
            "account": cmd["account"],
            "device_fp": cmd["device_fp"],
            "deadline": cmd["deadline"],
            "version": version,
        }
        seq = self._append("appointment_created", cmd, payload)
        storage.insert_appointment(
            {
                "appointment_id": cmd["appointment_id"],
                "account": cmd["account"],
                "device_fp": cmd["device_fp"],
                "deadline": cmd["deadline"],
                "version": version,
                "status": "ACTIVE",
            },
            seq,
        )
        storage.add_evidence(
            cmd["appointment_id"], "appointed", self._tick(cmd), [],
            {"deadline": cmd["deadline"], "version": version}, seq,
        )
        return "OK", seq

    def _h_approve(self, cmd):
        storage = self.storage
        appt = storage.get_appointment(cmd["appointment_id"])
        if appt is None:
            return "APPOINTMENT_NOT_FOUND", None
        if appt["status"] == "FULFILLED":
            return "ALREADY_FULFILLED", None
        active = storage.active_version()
        if active is None:
            return "NOT_INITIALIZED", None
        # 审批绑定哪个版本，就按哪个版本的配置校验见证员；
        # 待转交期间允许补旧版本见证以满足旧门槛，也允许提前给新版本确认。
        version = cmd.get("version", active)
        config = storage.get_config(version)
        if config is None:
            return "VERSION_NOT_DEFINED", None
        if appt["status"] == "ACTIVE":
            # 普通预约只能给当前适用版本加审批
            if version != appt["version"]:
                return "VERSION_NOT_ACTIVE", None
        elif appt["status"] == "PENDING_HANDOVER":
            # 待转交：仅接受旧版本（补门槛）或新生效版本（转交确认）
            if version not in (appt["version"], active):
                return "VERSION_NOT_ACTIVE", None
        witness = cmd["witness"]
        group_name = next(
            (g for g, roster in config["members"].items() if witness in roster),
            None,
        )
        if group_name is None:
            return "WITNESS_UNKNOWN", None
        if storage.is_revoked(witness):
            return "ALREADY_REVOKED", None
        existing = storage.find_approval(
            cmd["appointment_id"], witness, version
        )
        if existing is not None:
            if existing["content"] == cmd["content"]:
                # 同见证员同内容重复审批：幂等成功，不追加事件
                return "OK", existing["created_event"]
            return "CONFLICTING_APPROVAL", None

        payload = {
            "appointment_id": cmd["appointment_id"],
            "witness": witness,
            "version": version,
            "group_name": group_name,
            "content": cmd["content"],
        }
        seq = self._append("approval_recorded", cmd, payload)
        storage.insert_approval(
            cmd["appointment_id"], witness, version, group_name,
            cmd["content"], seq,
        )
        _, covered, detail = evaluate_threshold(
            storage, cmd["appointment_id"], version
        )
        storage.add_evidence(
            cmd["appointment_id"], "approval_added", self._tick(cmd),
            covered, detail, seq,
        )
        return "OK", seq

    def _h_revoke(self, cmd):
        storage = self.storage
        witness = cmd["witness"]
        if storage.is_revoked(witness):
            # 撤销本身幂等：返回原撤销事件，不追加新事件
            row = storage.conn.execute(
                "SELECT revoked_event FROM revocations WHERE witness = ?",
                (witness,),
            ).fetchone()
            return "OK", row["revoked_event"]
        known = any(
            witness in roster
            for cfg in storage.list_configs()
            for roster in cfg["members"].values()
        )
        if not known:
            return "WITNESS_UNKNOWN", None
        seq = self._append("witness_revoked", cmd, {"witness": witness})
        storage.insert_revocation(witness, seq)
        # 撤销自下一条事件起生效；为持有该见证员未取代审批的未兑现预约留存证据
        for appt in storage.list_appointments():
            if appt["status"] == "FULFILLED":
                continue
            own_rows = storage.conn.execute(
                "SELECT 1 FROM approvals WHERE appointment_id = ? "
                "AND witness = ? AND superseded = 0 LIMIT 1",
                (appt["appointment_id"], witness),
            ).fetchall()
            if not own_rows:
                continue
            _, covered, detail = evaluate_threshold(
                storage, appt["appointment_id"], appt["version"]
            )
            storage.add_evidence(
                appt["appointment_id"], "witness_revoked",
                self._tick(cmd), covered, detail, seq,
            )
        return "OK", seq

    def _h_handover(self, cmd):
        storage = self.storage
        appt = storage.get_appointment(cmd["appointment_id"])
        if appt is None:
            return "APPOINTMENT_NOT_FOUND", None
        if appt["status"] != "PENDING_HANDOVER":
            # 已成功转交过的预约此刻是 ACTIVE：按“不在待转交状态”拒绝；
            # 若再次切换重新进入待转交，才由下一行的次数约束拦截。
            return "HANDOVER_NOT_PENDING", None
        if appt["handover_count"] >= 1:
            return "ALREADY_HANDED_OVER", None

        old_version = appt["version"]
        new_version = storage.active_version()
        old_met, old_covered, old_detail = evaluate_threshold(
            storage, appt["appointment_id"], old_version
        )
        if not old_met:
            return "THRESHOLD_NOT_MET", None

        new_approvals = storage.valid_approvals(
            appt["appointment_id"], new_version
        )
        new_config = storage.get_config(new_version)
        new_chair_witnesses = [
            a["witness"] for a in new_approvals
            if a["group_name"] == new_config["chair_group"]
        ]
        if not new_chair_witnesses:
            return "CHAIR_MISSING", None

        old_config = storage.get_config(old_version)
        old_chair_witnesses = [
            a["witness"]
            for a in storage.valid_approvals(appt["appointment_id"], old_version)
            if a["group_name"] == old_config["chair_group"]
        ]
        # 禁止同一人同时满足两边负责人资格：必须能配出两个不同的人
        if not any(o != n for o in old_chair_witnesses for n in new_chair_witnesses):
            return "SAME_CHAIR_BOTH_SIDES", None

        payload = {
            "appointment_id": cmd["appointment_id"],
            "from_version": old_version,
            "to_version": new_version,
            "old_chair_witness": old_chair_witnesses[0],
            "new_chair_witness": next(
                n for n in new_chair_witnesses
                if n != old_chair_witnesses[0]
            ),
        }
        seq = self._append("committee_handed_over", cmd, payload)
        storage.supersede_approvals(appt["appointment_id"], old_version)
        storage.update_appointment(
            appt["appointment_id"],
            version=new_version,
            status="ACTIVE",
            handover_count=appt["handover_count"] + 1,
        )
        _, new_covered, new_detail = evaluate_threshold(
            storage, appt["appointment_id"], new_version
        )
        storage.add_evidence(
            appt["appointment_id"], "handed_over", self._tick(cmd),
            new_covered,
            {
                "from_version": old_version,
                "to_version": new_version,
                "old_groups": old_covered,
                "new_groups": new_covered,
                "old_detail": old_detail,
                "new_detail": new_detail,
            },
            seq,
        )
        return "OK", seq

    def _h_fulfill(self, cmd):
        storage = self.storage
        appt = storage.get_appointment(cmd["appointment_id"])
        if appt is None:
            return "APPOINTMENT_NOT_FOUND", None

        # 已兑现：登记仍在则幂等成功；登记已撤销则禁止复活
        if appt["status"] == "FULFILLED":
            reg = storage.registration_for_device(appt["device_fp"])
            if reg is not None and reg["status"] == "revoked":
                return "DEVICE_REVIVED_FORBIDDEN", None
            return "OK", reg["fulfill_event"] if reg else None

        # 失败原因优先级：过期 > 未转交 > 门槛不足 > 设备占用/禁止复活
        if storage.current_clock() >= appt["deadline"]:
            return "EXPIRED", None
        if appt["status"] == "PENDING_HANDOVER":
            return "NOT_HANDED_OVER", None
        met, covered, detail = evaluate_threshold(
            storage, appt["appointment_id"], appt["version"]
        )
        if not met:
            return "THRESHOLD_NOT_MET", None

        reg = storage.registration_for_device(appt["device_fp"])
        if reg is not None:
            if reg["status"] == "current":
                return "DEVICE_OCCUPIED", None
            return "DEVICE_REVIVED_FORBIDDEN", None

        payload = {
            "appointment_id": cmd["appointment_id"],
            "account": appt["account"],
            "device_fp": appt["device_fp"],
            "version": appt["version"],
        }
        seq = self._append("authenticator_fulfilled", cmd, payload)
        try:
            storage.insert_registration(
                appt["appointment_id"], appt["account"],
                appt["device_fp"], appt["version"], seq,
            )
        except sqlite3.IntegrityError:
            # 并发兜底：部分唯一索引拦截重复占用。事件尚未提交，直接撤回本条事件，
            # 以普通业务拒绝（DEVICE_OCCUPIED）出回执，批次其余部分照常发布。
            storage.conn.execute("DELETE FROM events WHERE seq = ?", (seq,))
            return "DEVICE_OCCUPIED", None
        storage.update_appointment(cmd["appointment_id"], status="FULFILLED")
        storage.add_evidence(
            appt["appointment_id"], "fulfilled", self._tick(cmd),
            covered, detail, seq,
        )
        return "OK", seq

    def _h_revoke_registration(self, cmd):
        storage = self.storage
        device_fp = cmd["device_fp"]
        reg = storage.registration_for_device(device_fp)
        if reg is None:
            return "REGISTRATION_NOT_FOUND", None
        if reg["status"] == "revoked":
            return "REGISTRATION_REVOKED", None
        seq = self._append(
            "registration_revoked", cmd,
            {"device_fp": device_fp, "appointment_id": reg["appointment_id"]},
        )
        storage.mark_registration_revoked(device_fp, seq)
        storage.add_evidence(
            reg["appointment_id"], "registration_revoked", self._tick(cmd),
            [], {"device_fp": device_fp}, seq,
        )
        return "OK", seq

    def _h_tick(self, cmd):
        """纯时钟推进事件：不改变任何业务状态，专门用于同 tick 边界测试。"""
        seq = self._append("clock_advanced", cmd, {})
        return "OK", seq
