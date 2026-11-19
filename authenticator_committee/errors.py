"""异常类型与业务回执原因码（原因码稳定可查询，附中文说明）。"""


class InvalidBatch(Exception):
    """整批输入结构性无效，必须整批回滚、不产生任何事件或回执。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or [message]


class EngineFault(Exception):
    """测试用注入故障：在提交前抛出，事务必须回滚。"""


# 原因码 -> 中文说明。原因码本身是稳定契约，测试与夹具直接引用。
REASON_TEXT = {
    "OK": "成功",
    "NOT_INITIALIZED": "委员会尚未初始化",
    "ALREADY_INITIALIZED": "委员会已初始化，不能重复 init",
    "UNKNOWN_COMMAND": "未知命令",
    "VERSION_NOT_DEFINED": "目标配置版本尚未定义",
    "VERSION_ALREADY_DEFINED": "配置版本已存在，不能重复定义",
    "VERSION_NOT_ACTIVE": "审批版本不是预约当前适用的生效版本",
    "SWITCH_SAME_VERSION": "切换目标版本与当前生效版本相同",
    "DUPLICATE_APPOINTMENT": "预约 ID 已存在",
    "APPOINTMENT_NOT_FOUND": "预约不存在",
    "DEVICE_OCCUPIED": "设备指纹已被其他当前账户占用",
    "DEVICE_REVIVED_FORBIDDEN": "该设备的登记已被撤销，禁止直接重新激活",
    "REGISTRATION_NOT_FOUND": "设备没有可撤销的登记",
    "REGISTRATION_REVOKED": "登记此前已撤销",
    "EXPIRED": "逻辑时钟已达到登记截止值，预约失效",
    "THRESHOLD_NOT_MET": "有效见证去重后的组数不足或缺少负责人组",
    "NOT_HANDED_OVER": "预约在旧版本上处于待转交状态，必须先完成转交",
    "HANDOVER_NOT_PENDING": "预约不处于待转交状态，不能执行转交",
    "ALREADY_HANDED_OVER": "该预约已经转交过一次，不能再次转交",
    "ALREADY_FULFILLED": "预约已兑现，不能重复兑现或追加审批",
    "ALREADY_REVOKED": "该见证员已被撤销",
    "WITNESS_UNKNOWN": "见证员不属于该配置版本",
    "CONFLICTING_APPROVAL": "同一见证员已提交内容不同的审批，变更冲突",
    "CHAIR_MISSING": "缺少新版本负责人组的确认",
    "SAME_CHAIR_BOTH_SIDES": "同一人不能同时满足新旧两版本的负责人资格",
    "TICK_OUT_OF_ORDER": "逻辑时钟不能倒退（tick 必须不小于当前时钟）",
}
