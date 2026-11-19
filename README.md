# 认证器登记委员会模拟器

一个**纯本地、零外部依赖**的认证器登记委员会模拟器：员工经多名独立见证员确认后登记一枚
合成认证器，委员会可跨配置版本交接，设备登记历史全程可审计。仅使用 Python 3.12 标准库
与 SQLite；所有身份、设备指纹、审批内容均为虚构测试数据，不实现任何签名算法，也不接触
系统进程。

## 1. 快速开始

环境：Windows 原生 Python 3.12（任何 3.10+ 亦可），无需安装第三方包。

```bash
# 运行全部自动化测试（42 个用例）
python -I run_tests.py

# 四个命令行入口（-I 为隔离模式，避免从当前目录误载入恶意同名模块）
python -I authenticator_committee/cli.py init    --db demo.db --input fixtures/01_init.json
python -I authenticator_committee/cli.py apply   --db demo.db --input fixtures/02_appoint.json
python -I authenticator_committee/cli.py inspect --db demo.db
python -I authenticator_committee/cli.py verify  --db demo.db
```

> `-I`（隔离模式）下解释器不自动把当前目录加入 `sys.path`，因此 CLI 脚本内置了路径
> 自举，可直接以脚本路径启动；测试统一通过根目录 `run_tests.py` 注入路径。

## 2. 命令与数据模型

### 2.1 四个 CLI 命令

| 命令 | 作用 |
| --- | --- |
| `init` | 初始化委员会，写入 v1 配置（`--input` 省略时使用内置最小配置） |
| `apply` | 读取一个 JSON 批次，在**单事务**内原子应用，输出逐命令回执 |
| `inspect` | 查询当前时钟、生效版本、预约/登记/设备占用/撤销名单、回执与逐阶段证据、原始事件 |
| `verify` | 仅依据 `events` 表重放重建状态，与物化表逐字段交叉核验，并校验哈希链 |

退出码：`0` 成功（业务拒绝不算失败）、`2` 输入/用法错误（整批回滚）、
`3` 核验不通过、`4` 其他错误。

### 2.2 委员会配置

```json
{
  "version": 1,
  "required_groups": 3,
  "chair_group": "G1",
  "members": { "G1": ["alice"], "G2": ["bob"], "G3": ["carol", "frank"] }
}
```

- `version`：配置版本号（正整数）；`members` 为独立组 -> 见证员名单，同一配置内见证员唯一。
- `required_groups`：有效见证按组去重后必须覆盖的最少组数。
- `chair_group`：负责人组，有效见证必须覆盖该组。

### 2.3 批次 JSON 与命令词汇

```json
{
  "batch_id": "fixture-appoint",
  "commands": [
    {"cmd": "appoint", "appointment_id": "APPT-1", "account": "acct-1",
     "device_fp": "fp-AAA", "deadline": 50}
  ]
}
```

| `cmd` | 关键字段 | 说明 |
| --- | --- | --- |
| `init` | `config` | 初始化（仅一次） |
| `define_config` | `config` | 定义新版本，不立即生效 |
| `switch` | `version` | 切换生效版本；所有未兑现预约进入待转交 |
| `appoint` | `appointment_id/account/device_fp/deadline` | 创建预约，绑定当前配置版本与截止时钟 |
| `approve` | `appointment_id/witness/content[/version]` | 见证审批，绑定预约与配置版本 |
| `revoke` | `witness` | 撤销见证员，自下一条事件起生效 |
| `handover` | `appointment_id` | 完成一次版本转交 |
| `fulfill` | `appointment_id` | 兑现登记 |
| `revoke_registration` | `device_fp` | 撤销一枚当前登记，保留历史 |
| `tick` | 可选 `tick` | 纯时钟推进事件，用于同 tick 边界测试 |

任何命令都可带可选 `tick`（正整数）显式指定事件时钟；缺省取当前时钟 +1。
`tick` 倒退是业务拒绝（`TICK_OUT_OF_ORDER`），不产生事件。

## 3. 核心业务规则

### 3.1 见证与门槛

- `approve` 必须绑定预约与配置版本；审批按**所绑定版本**的配置校验见证员归属。
- 同一见证员对同一预约同一版本重复提交**相同内容**：幂等成功，不新增事件；
  内容不同：`CONFLICTING_APPROVAL`。
- 有效见证 = 未被取代且见证员未被撤销；按组去重后，组数须达到 `required_groups`
  且必须覆盖 `chair_group`。
- 已撤销人员不能再确认（`ALREADY_REVOKED`）。

### 3.2 撤销的生效边界

撤销事件本身之后，该见证员**尚未兑现的审批立即失效**（下一条事件起生效）；
已完成的登记保留全部历史，不受影响。撤销操作幂等。

### 3.3 委员会交接

- `switch` 使所有未兑现预约进入 `PENDING_HANDOVER`；已兑现预约不受影响。
- `handover` 要求：
  1. **旧版本**见证仍满足旧门槛；
  2. **新版本**负责人组另有确认（`CHAIR_MISSING` 否则拒绝）；
  3. 不能由**同一人**同时满足新旧两边的负责人资格
     （`SAME_CHAIR_BOTH_SIDES`）——只要能配出两个不同的人即可；
  4. 一枚预约只能转交一次（再次切换后重提将得 `ALREADY_HANDED_OVER`）。
- 转交成功后旧版本审批全部标记 `superseded`，必须在新版本**重新收集见证**。
- 两版本共享的组可分别用于各自版本的门槛，但同一人不能同时充当两边负责人。

### 3.4 逻辑时钟与失效边界

- 时钟完全由事件驱动：每条被接受的命令追加一个事件，事件 `tick` 即当前时钟。
- `clock == deadline` 时预约即失效（兑现返回 `EXPIRED`）；同 tick 内的先后由
  事件序号 `seq` 唯一确定（见 `tests/test_clock.py`）。

### 3.5 设备指纹唯一性

- 同一设备指纹全局至多对应**一个当前账户**（数据库层部分唯一索引兜底）。
- 第一个兑现成功者占用设备；后来者得 `DEVICE_OCCUPIED`。
- 登记被撤销后，同指纹的新预约/兑现一律 `DEVICE_REVIVED_FORBIDDEN`，禁止直接复活。
- 兑现失败原因优先级：**已兑现(幂等) → 过期 → 未转交 → 门槛不足 → 设备占用/禁止复活**。

### 3.6 批次原子性与回执

- **结构性输入无效**（缺字段、类型错误、未知命令等）：抛 `InvalidBatch`，整批回滚，
  不留下任何事件、批次或回执。
- **合法业务拒绝**（门槛不足、过期、设备占用等）：不回滚，逐条写入可查询回执，
  原因码稳定（见 `authenticator_committee/errors.py` 的 `REASON_TEXT`）。
- 批次 ID 唯一；同一批次重复提交为幂等重放，返回原回执且不新增事件。
- 两进程以 `BEGIN IMMEDIATE` + `busy_timeout` 串行化提交，配合设备部分唯一索引，
  保证不重复兑现、不产生部分记录、失败批次不会提前发布。

## 4. 架构：事件溯源 + 物化双轨

```
authenticator_committee/
├── storage.py     # SQLite 建表、只追加事件日志（SHA-256 哈希链）、物化表与查询
├── engine.py      # 输入校验、业务规则、事件驱动时钟、单批次事务、故障注入钩子
├── projection.py  # 仅读 events 重放重建状态，并与物化表交叉核验
├── enumeration.py # 小状态枚举 + 手写真值表，独立核验转交/兑现判定
├── errors.py      # 异常与稳定原因码
└── cli.py         # init / apply / inspect / verify
```

- **事件表 `events`** 是唯一事实源，载荷以规范化 JSON 存储并参与 SHA-256 哈希链；
  其余表（配置、预约、审批、撤销、登记、证据、回执）都是派生视图。
- **重启恢复**：重新打开数据库即得物化状态；`verify` 用独立内存模型重放事件并逐字段
  比对，任何篡改或漂移都会在报告的 `discrepancies` 中列出。
- **逐阶段见证证据**（`stage_evidence`）：预约、每次审批后的覆盖组、切换待转交、
  撤销影响、转交、兑现、登记撤销均留痕，`inspect` 可直接审计。

## 5. 独立枚举核验

`enumeration.py` 不调用引擎的任何判定函数，而是：

1. 用 `enum.Enum` 定义转交/兑现的最小状态空间（预约状态 × 时钟 × 门槛 × 设备态等）；
2. 以手写判定表（规则原文直译）给出每个组合的期望结论；
3. 在真实 SQLite 上把每个组合搭成实际场景跑引擎，比对回执原因码。

共枚举 **36 个兑现组合（其中 25 个可达，其余为现实互斥的不可达组合，显式跳过并
注明理由）+ 12 个转交组合**，由 `tests/test_cli_enum.py` 断言零偏差。

## 6. 测试与覆盖

```
tests/
├── helpers.py              # 临时库、虚构配置、批次构造助手
├── test_storage_batch.py   # 整批回滚、幂等重放、提交前故障注入与重试、哈希链篡改、重启重建
├── test_handover.py        # 跨组人员变更、共享组复用、负责人同一人禁令、只能转交一次
├── test_revocation.py      # 撤销与兑现交错、后备补位、已完成登记保留历史
├── test_clock.py           # clock==deadline 失效、同 tick 按 seq 判定、时钟不可倒退
├── test_device.py          # 双账户争同一设备、撤销后禁止复活、全局单一当前账户
├── test_concurrency.py     # 真双进程并发提交（subprocess），不重复兑现、无部分记录
└── test_cli_enum.py        # CLI 端到端、退出码、枚举真值表交叉核验
```

题目要求的场景一一对应：

| 要求场景 | 覆盖用例 |
| --- | --- |
| 跨组人员变更 | `test_handover.py` 全部 |
| 两版本负责人为同一人 | `test_same_person_cannot_be_chair_on_both_sides` |
| 撤销与兑现交错 | `test_revocation.py` 全部 |
| 同 tick 边界 | `test_clock.py::test_same_tick_*`、`test_at_deadline_*` |
| 双账户争同一设备 | `test_device.py`、`test_concurrency.py` |
| 批次尾故障后重试 | `test_storage_batch.py::test_fault_*` |
| 小状态枚举独立核验 | `test_cli_enum.py::TestEnumerationCrossCheck` |

## 7. 回执原因码（稳定契约）

`OK` 之外的主要拒绝码：`EXPIRED`、`THRESHOLD_NOT_MET`、`NOT_HANDED_OVER`、
`DEVICE_OCCUPIED`、`DEVICE_REVIVED_FORBIDDEN`、`CHAIR_MISSING`、
`SAME_CHAIR_BOTH_SIDES`、`ALREADY_HANDED_OVER`、`ALREADY_REVOKED`、
`CONFLICTING_APPROVAL`、`WITNESS_UNKNOWN`、`TICK_OUT_OF_ORDER` 等，
中文说明见 `errors.REASON_TEXT`，可通过 `inspect` 的 `receipts` 查询。
