"""并发测试用子进程工作脚本（由 test_concurrency.py 以 subprocess 启动）。

参数：<db路径> <批次JSON文件>
两个进程同时对同一 SQLite 文件提交“预约+全套见证+兑现”批次，
竞争同一设备指纹，验证只有一个进程兑现成功。
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from authenticator_committee.engine import CommitteeEngine
from authenticator_committee.storage import Storage


def main():
    # 子进程默认按系统代码页（GBK）输出，测试端按 UTF-8 解析，这里显式统一
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db_path, batch_path = sys.argv[1], sys.argv[2]
    batch = json.loads(pathlib.Path(batch_path).read_text(encoding="utf-8"))
    storage = Storage(db_path)
    try:
        result = CommitteeEngine(storage).apply_batch(batch)
    finally:
        storage.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
