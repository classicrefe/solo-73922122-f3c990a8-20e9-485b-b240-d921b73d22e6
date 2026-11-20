"""共享中间表示（IR）与固定模式定义。

固定模式（全列 integer，非主键列可 NULL）：
    stations(id INTEGER PRIMARY KEY, threshold INTEGER)
    readings(station_id INTEGER REFERENCES stations(id), value INTEGER)
"""
from __future__ import annotations

from dataclasses import dataclass

# 表 -> 列（按定义顺序）
SCHEMA: dict[str, tuple[str, ...]] = {
    "stations": ("id", "threshold"),
    "readings": ("station_id", "value"),
}
PRIMARY_KEY: dict[str, str] = {"stations": "id"}
# (子表, 外键列, 父表, 父表主键列)
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("readings", "station_id", "stations", "id"),
)

# PostgreSQL INTEGER (int4) 取值范围
INT_MIN = -(2**31)
INT_MAX = 2**31 - 1

# 有界等价检查的每表行数上界
MAX_ROWS = 3


# ---------- 布尔表达式 IR ----------
@dataclass(frozen=True)
class ColRef:
    table: str
    column: str


@dataclass(frozen=True)
class Compare:
    col: ColRef
    op: str  # '=', '!=', '<', '<=', '>', '>='
    value: int


@dataclass(frozen=True)
class IsNull:
    col: ColRef
    negated: bool


@dataclass(frozen=True)
class And:
    left: "BoolNode"
    right: "BoolNode"


@dataclass(frozen=True)
class Or:
    left: "BoolNode"
    right: "BoolNode"


@dataclass(frozen=True)
class Not:
    child: "BoolNode"


BoolNode = Compare | IsNull | And | Or | Not


@dataclass(frozen=True)
class JoinSpec:
    kind: str  # 'inner' | 'left'
    left_col: ColRef   # 属于驱动表（FROM 表）
    right_col: ColRef  # 属于被连接表


@dataclass(frozen=True)
class QueryIR:
    tables: tuple[str, ...]           # 1~2 张表；连接时驱动表在前
    join: JoinSpec | None
    projections: tuple[ColRef, ...]   # 按列位置
    where: BoolNode | None
