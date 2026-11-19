"""本地认证器登记委员会模拟器（仅标准库 + SQLite，全部为虚构测试数据）。

模块划分：
- storage:     SQLite 连接、建表、事件追加与哈希链
- engine:      批处理引擎，命令校验、业务规则、事件驱动逻辑时钟
- projection:  仅依据原始事件重放状态，并与物化表交叉核验
- enumeration: 小状态枚举 + 手写真值表，独立核验转交/兑现判定
- cli:         init / apply / inspect / verify 四个命令行入口
"""

__version__ = "1.0.0"
