"""命令行入口：init / apply / inspect / verify。

用法：
    python -m authenticator_committee.cli init    --db <path> [--input init.json]
    python -m authenticator_committee.cli apply   --db <path> --input batch.json
    python -m authenticator_committee.cli inspect --db <path> [--batch B] [--rejected]
    python -m authenticator_committee.cli verify  --db <path>

退出码：0 成功（业务拒绝不算失败）；2 输入/用法错误；3 核验不通过；4 其他错误。
"""

import argparse
import json
import sys

# Windows 控制台默认 GBK，统一按 UTF-8 输出，避免中文回执乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# 允许 `python authenticator_committee/cli.py` 与 `python -m ...cli` 两种启动方式
if __package__ in (None, ""):
    sys.path.insert(0, _parent := __import__("os").path.dirname(
        __import__("os").path.dirname(__import__("os").path.abspath(__file__))))
    from authenticator_committee.engine import CommitteeEngine
    from authenticator_committee.errors import InvalidBatch
    from authenticator_committee.projection import verify as run_verify
    from authenticator_committee.storage import Storage
else:
    from .engine import CommitteeEngine
    from .errors import InvalidBatch
    from .projection import verify as run_verify
    from .storage import Storage

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VERIFY_FAILED = 3
EXIT_ERROR = 4


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_init(args):
    if args.input:
        payload = _load_json(args.input)
        if not isinstance(payload, dict) or "config" not in payload:
            raise InvalidBatch("init 输入必须是含 config 字段的对象")
        batch = {"batch_id": payload.get("batch_id", "cli-init"),
                 "commands": [{"cmd": "init", "config": payload["config"]}]}
    else:
        # 无输入时给出一个最小虚构默认配置，便于快速起步
        batch = {
            "batch_id": "cli-default-init",
            "commands": [{
                "cmd": "init",
                "config": {
                    "version": 1,
                    "required_groups": 2,
                    "chair_group": "G1",
                    "members": {
                        "G1": ["alice"],
                        "G2": ["bob"],
                    },
                },
            }],
        }
    storage = Storage(args.db)
    try:
        result = CommitteeEngine(storage).apply_batch(batch)
    finally:
        storage.close()
    return result


def _cmd_apply(args):
    batch = _load_json(args.input)
    storage = Storage(args.db)
    try:
        result = CommitteeEngine(storage).apply_batch(batch)
    finally:
        storage.close()
    return result


def _cmd_inspect(args):
    storage = Storage(args.db)
    try:
        active = storage.active_version()
        appointments = []
        for appt in storage.list_appointments():
            expired = storage.current_clock() >= appt["deadline"]
            appointments.append({
                **appt,
                "expired": expired,
                "effective_status": "EXPIRED" if expired and
                appt["status"] != "FULFILLED" else appt["status"],
            })
        report = {
            "clock": storage.current_clock(),
            "active_version": active,
            "configs": storage.list_configs(),
            "appointments": appointments,
            "registrations": storage.list_registrations(),
            "current_devices": {
                r["device_fp"]: r["account"]
                for r in storage.list_registrations() if r["status"] == "current"
            },
            "revoked_witnesses": [r["witness"] for r in storage.list_revocations()],
            "receipts": storage.list_receipts(
                batch_id=args.batch, rejected_only=args.rejected
            ),
            "evidence": storage.list_evidence(),
            "events": storage.all_events(),
        }
    finally:
        storage.close()
    return report


def _cmd_verify(args):
    storage = Storage(args.db)
    try:
        ok, report = run_verify(storage)
    finally:
        storage.close()
    return ok, report


def build_parser():
    parser = argparse.ArgumentParser(
        prog="authenticator_committee",
        description="本地认证器登记委员会模拟器（虚构测试数据，标准库实现）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化委员会与 v1 配置")
    p_init.add_argument("--db", required=True)
    p_init.add_argument("--input", help="含 config 的 JSON 文件；省略则用默认配置")

    p_apply = sub.add_parser("apply", help="原子应用一个 JSON 命令批次")
    p_apply.add_argument("--db", required=True)
    p_apply.add_argument("--input", required=True)

    p_inspect = sub.add_parser("inspect", help="查询可见状态、回执与逐阶段证据")
    p_inspect.add_argument("--db", required=True)
    p_inspect.add_argument("--batch", help="只看指定批次回执")
    p_inspect.add_argument("--rejected", action="store_true",
                           help="回执只保留业务拒绝项")

    p_verify = sub.add_parser("verify", help="事件重放并交叉核验物化状态")
    p_verify.add_argument("--db", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps(_cmd_init(args), ensure_ascii=False, indent=2))
        elif args.command == "apply":
            print(json.dumps(_cmd_apply(args), ensure_ascii=False, indent=2))
        elif args.command == "inspect":
            print(json.dumps(_cmd_inspect(args), ensure_ascii=False, indent=2))
        elif args.command == "verify":
            ok, report = _cmd_verify(args)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return EXIT_OK if ok else EXIT_VERIFY_FAILED
        return EXIT_OK
    except InvalidBatch as exc:
        print(json.dumps(
            {"rolled_back": True, "errors": exc.errors},
            ensure_ascii=False, indent=2,
        ), file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"文件不存在：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
