"""SQLite 存储层。

设计要点：
- events 为只追加事件日志，payload 以规范化 JSON 存储并参与 SHA-256 哈希链；
- 其余表是由事件派生出的物化视图，便于查询与加约束；verify 时会用事件重放重建并交叉核验；
- 部分唯一索引保证同一设备指纹全局至多一个 current 登记；
- 所有写入在单连接单事务中完成，apply 以 BEGIN IMMEDIATE 立即拿写锁，
  配合 busy_timeout 实现两进程串行提交，杜绝重复兑现与部分记录。
"""

import hashlib
import json
import sqlite3

GENESIS_HASH = "GENESIS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    tick        INTEGER NOT NULL,
    batch_id    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    event_hash  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    created_event   INTEGER NOT NULL,
    last_event      INTEGER NOT NULL,
    command_count   INTEGER NOT NULL,
    published       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS configs (
    version            INTEGER PRIMARY KEY,
    required_groups    INTEGER NOT NULL,
    chair_group        TEXT NOT NULL,
    members_json       TEXT NOT NULL,
    defined_event      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS active_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id   TEXT PRIMARY KEY,
    account          TEXT NOT NULL,
    device_fp        TEXT NOT NULL,
    deadline         INTEGER NOT NULL,
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    handover_count   INTEGER NOT NULL DEFAULT 0,
    created_event    INTEGER NOT NULL,
    updated_event    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id    TEXT NOT NULL,
    witness           TEXT NOT NULL,
    version           INTEGER NOT NULL,
    group_name        TEXT NOT NULL,
    content           TEXT NOT NULL,
    superseded        INTEGER NOT NULL DEFAULT 0,
    created_event     INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_approval_once
    ON approvals(appointment_id, witness, version, content);
CREATE INDEX IF NOT EXISTS ix_approval_appt ON approvals(appointment_id);

CREATE TABLE IF NOT EXISTS revocations (
    witness        TEXT PRIMARY KEY,
    revoked_event  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS registrations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id   TEXT NOT NULL,
    account          TEXT NOT NULL,
    device_fp        TEXT NOT NULL,
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    fulfill_event    INTEGER NOT NULL,
    revoke_event     INTEGER
);
-- 同一设备指纹全局只能有一个当前（current）登记
CREATE UNIQUE INDEX IF NOT EXISTS ux_device_current
    ON registrations(device_fp) WHERE status = 'current';
CREATE INDEX IF NOT EXISTS ix_reg_device ON registrations(device_fp);

CREATE TABLE IF NOT EXISTS stage_evidence (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id   TEXT NOT NULL,
    stage            TEXT NOT NULL,
    tick             INTEGER NOT NULL,
    groups_json      TEXT NOT NULL,
    detail_json      TEXT NOT NULL,
    event_seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_appt ON stage_evidence(appointment_id);

CREATE TABLE IF NOT EXISTS receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL,
    command_index   INTEGER NOT NULL,
    command         TEXT NOT NULL,
    ref_id          TEXT,
    accepted        INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    message         TEXT NOT NULL,
    event_seq       INTEGER
);
"""


def canonical_json(obj):
    """确定性 JSON 序列化：键排序、无多余空白，保证哈希稳定。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_event_hash(prev_hash, event_type, tick, payload):
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(b"|")
    digest.update(event_type.encode("utf-8"))
    digest.update(b"|")
    digest.update(str(tick).encode("ascii"))
    digest.update(b"|")
    digest.update(canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


class Storage:
    """单个 SQLite 数据库的连接封装。一个 Storage 实例对应一个连接、一个线程。"""

    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # 两进程并发提交时，第二个进程等待写锁而不是立刻报 locked
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 事务边界 -------------------------------------------------
    def begin_immediate(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    # ---- 事件日志 -------------------------------------------------
    def last_event_seq(self):
        row = self.conn.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        return row["m"] if row["m"] is not None else 0

    def last_event_hash(self):
        row = self.conn.execute(
            "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["event_hash"] if row else GENESIS_HASH

    def append_event(self, ts, event_type, tick, batch_id, payload):
        """追加事件；seq 显式取自当前最大序号 +1，保证顺序与哈希链确定。"""
        seq = self.last_event_seq() + 1
        prev_hash = self.last_event_hash()
        payload_text = canonical_json(payload)
        event_hash = compute_event_hash(prev_hash, event_type, tick, payload)
        self.conn.execute(
            "INSERT INTO events(seq, ts, event_type, tick, batch_id, payload, "
            "prev_hash, event_hash) VALUES (?,?,?,?,?,?,?,?)",
            (seq, ts, event_type, tick, batch_id, payload_text, prev_hash, event_hash),
        )
        return seq, event_hash

    def all_events(self):
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        ]

    # ---- 批次 -----------------------------------------------------
    def get_batch(self, batch_id):
        row = self.conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_batch(self, batch_id, first_event, last_event, command_count):
        self.conn.execute(
            "INSERT INTO batches(batch_id, created_event, last_event, "
            "command_count, published) VALUES (?,?,?,?,1)",
            (batch_id, first_event, last_event, command_count),
        )

    # ---- 配置 -----------------------------------------------------
    def get_config(self, version):
        row = self.conn.execute(
            "SELECT * FROM configs WHERE version = ?", (version,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["members"] = json.loads(data.pop("members_json"))
        return data

    def insert_config(self, version, required_groups, chair_group, members, event_seq):
        self.conn.execute(
            "INSERT INTO configs(version, required_groups, chair_group, "
            "members_json, defined_event) VALUES (?,?,?,?,?)",
            (
                version,
                required_groups,
                chair_group,
                canonical_json(members),
                event_seq,
            ),
        )

    def active_version(self):
        row = self.conn.execute("SELECT version FROM active_config WHERE id = 1").fetchone()
        return row["version"] if row else None

    def set_active_version(self, version):
        self.conn.execute(
            "INSERT INTO active_config(id, version) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
            (version,),
        )

    def list_configs(self):
        rows = self.conn.execute("SELECT * FROM configs ORDER BY version").fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["members"] = json.loads(data.pop("members_json"))
            out.append(data)
        return out

    # ---- 预约 -----------------------------------------------------
    def get_appointment(self, appointment_id):
        row = self.conn.execute(
            "SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_appointment(self, appt, event_seq):
        self.conn.execute(
            "INSERT INTO appointments(appointment_id, account, device_fp, deadline, "
            "version, status, handover_count, created_event, updated_event) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                appt["appointment_id"],
                appt["account"],
                appt["device_fp"],
                appt["deadline"],
                appt["version"],
                appt["status"],
                0,
                event_seq,
                event_seq,
            ),
        )

    def update_appointment(self, appointment_id, **fields):
        fields["updated_event"] = self.last_event_seq()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [appointment_id]
        self.conn.execute(
            f"UPDATE appointments SET {assignments} WHERE appointment_id = ?", params
        )

    def list_appointments(self):
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM appointments ORDER BY appointment_id"
            ).fetchall()
        ]

    # ---- 审批 / 撤销 ----------------------------------------------
    def find_approval(self, appointment_id, witness, version):
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE appointment_id = ? AND witness = ? "
            "AND version = ? ORDER BY id LIMIT 1",
            (appointment_id, witness, version),
        ).fetchone()
        return dict(row) if row else None

    def insert_approval(self, appointment_id, witness, version, group_name,
                        content, event_seq):
        self.conn.execute(
            "INSERT INTO approvals(appointment_id, witness, version, group_name, "
            "content, superseded, created_event) VALUES (?,?,?,?,?,0,?)",
            (appointment_id, witness, version, group_name, content, event_seq),
        )

    def supersede_approvals(self, appointment_id, version):
        self.conn.execute(
            "UPDATE approvals SET superseded = 1 "
            "WHERE appointment_id = ? AND version = ?",
            (appointment_id, version),
        )

    def valid_approvals(self, appointment_id, version):
        """某预约在指定版本上仍有效的审批：未被取代、见证员未被撤销。"""
        rows = self.conn.execute(
            "SELECT a.* FROM approvals a "
            "LEFT JOIN revocations r ON r.witness = a.witness "
            "WHERE a.appointment_id = ? AND a.version = ? AND a.superseded = 0 "
            "AND r.witness IS NULL ORDER BY a.id",
            (appointment_id, version),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_revoked(self, witness):
        row = self.conn.execute(
            "SELECT 1 FROM revocations WHERE witness = ?", (witness,)
        ).fetchone()
        return row is not None

    def insert_revocation(self, witness, event_seq):
        self.conn.execute(
            "INSERT INTO revocations(witness, revoked_event) VALUES (?,?)",
            (witness, event_seq),
        )

    def list_revocations(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM revocations ORDER BY witness").fetchall()]

    # ---- 登记 -----------------------------------------------------
    def registration_for_device(self, device_fp):
        row = self.conn.execute(
            "SELECT * FROM registrations WHERE device_fp = ? ORDER BY id DESC LIMIT 1",
            (device_fp,),
        ).fetchone()
        return dict(row) if row else None

    def insert_registration(self, appointment_id, account, device_fp, version, event_seq):
        self.conn.execute(
            "INSERT INTO registrations(appointment_id, account, device_fp, version, "
            "status, fulfill_event) VALUES (?,?,?,?,'current',?)",
            (appointment_id, account, device_fp, version, event_seq),
        )

    def mark_registration_revoked(self, device_fp, event_seq):
        self.conn.execute(
            "UPDATE registrations SET status = 'revoked', revoke_event = ? "
            "WHERE device_fp = ? AND status = 'current'",
            (event_seq, device_fp),
        )

    def list_registrations(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM registrations ORDER BY id").fetchall()]

    # ---- 见证证据 / 回执 ------------------------------------------
    def add_evidence(self, appointment_id, stage, tick, groups, detail, event_seq):
        self.conn.execute(
            "INSERT INTO stage_evidence(appointment_id, stage, tick, groups_json, "
            "detail_json, event_seq) VALUES (?,?,?,?,?,?)",
            (
                appointment_id,
                stage,
                tick,
                canonical_json(groups),
                canonical_json(detail),
                event_seq,
            ),
        )

    def list_evidence(self, appointment_id=None):
        if appointment_id is None:
            rows = self.conn.execute(
                "SELECT * FROM stage_evidence ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM stage_evidence WHERE appointment_id = ? ORDER BY id",
                (appointment_id,),
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["groups"] = json.loads(data.pop("groups_json"))
            data["detail"] = json.loads(data.pop("detail_json"))
            out.append(data)
        return out

    def add_receipt(self, batch_id, index, command, ref_id, accepted, reason,
                    message, event_seq=None):
        cur = self.conn.execute(
            "INSERT INTO receipts(batch_id, command_index, command, ref_id, accepted, "
            "reason, message, event_seq) VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, index, command, ref_id, 1 if accepted else 0, reason,
             message, event_seq),
        )
        return cur.lastrowid

    def list_receipts(self, batch_id=None, rejected_only=False):
        sql = "SELECT * FROM receipts WHERE 1=1"
        params = []
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if rejected_only:
            sql += " AND accepted = 0"
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- 当前时钟 -------------------------------------------------
    def current_clock(self):
        row = self.conn.execute(
            "SELECT tick FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row["tick"] if row else 0
