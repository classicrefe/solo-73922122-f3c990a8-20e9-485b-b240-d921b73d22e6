"""待补测读数影响分析：每站告警行数上下界的符号求解。

stations / readings 为具体快照；readings 中待补测的 NULL value 由 z3 变量代替，
约束在请求指定的整数闭区间内，其余数据照原样参与查询。对 stations 快照中的
每个站点，分别用 z3 Optimize 求质控查询结果中该站行数（多重集计数，重复行
照计）在所有合法补测取值下的最小值与最大值，并记录达到极值的一组完整补测值。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from z3 import (
    And as ZAnd,
    BoolVal,
    If,
    Int,
    IntVal,
    Not as ZNot,
    Optimize,
    Or as ZOr,
    is_int_value,
    sat,
    unknown as z3_unknown,
)

from .ir import QueryIR
from .z3check import Cell, encode_bool

# 输出符号行：(所属站点 id, 保留条件)
_OutRow = tuple[int, object]


@dataclass
class SnapshotStation:
    id: int
    threshold: int | None


@dataclass
class SnapshotReading:
    station_id: int | None
    value: int | None


@dataclass
class Extremum:
    """一个极值及达到它的一组完整补测值（readings 输入行号 -> 补测取值）。"""

    count: int
    fills: dict[int, int]


@dataclass
class StationBounds:
    station_id: int
    minimum: Extremum
    maximum: Extremum


@dataclass
class BoundsOutcome:
    status: str  # 'ok' | 'unknown'
    stations: list[StationBounds] = field(default_factory=list)
    detail: str = ""


def compute_station_bounds(
    stations: list[SnapshotStation],
    readings: list[SnapshotReading],
    fill_ranges: dict[int, tuple[int, int]],
    query: QueryIR,
    timeout_ms: int,
) -> BoundsOutcome:
    """求每站结果行数在所有合法补测取值下的最小 / 最大值。

    fill_ranges: readings 输入行号（0 起始）-> (下界, 上界)，覆盖且仅覆盖
    value 为 NULL 的待补测行（由调用方校验）。
    """
    fill_vars = {row: Int(f"fill_{row}") for row in sorted(fill_ranges)}
    out_rows = _output_rows(stations, readings, fill_vars, query)
    per_station: dict[int, list] = {s.id: [] for s in stations}
    for sid, kept in out_rows:
        per_station[sid].append(If(kept, 1, 0))

    results: list[StationBounds] = []
    for s in stations:
        count_expr = IntVal(0)
        for term in per_station[s.id]:
            count_expr = count_expr + term
        minimum, err = _optimize(count_expr, fill_vars, fill_ranges, "min", timeout_ms)
        if minimum is None:
            return BoundsOutcome(
                "unknown", detail=f"站点 {s.id} 行数最小值求解失败：{err}"
            )
        maximum, err = _optimize(count_expr, fill_vars, fill_ranges, "max", timeout_ms)
        if maximum is None:
            return BoundsOutcome(
                "unknown", detail=f"站点 {s.id} 行数最大值求解失败：{err}"
            )
        results.append(StationBounds(s.id, minimum, maximum))
    return BoundsOutcome("ok", stations=results)


# ---------- 快照的符号化 ----------

def _const_cell(v: int | None) -> Cell:
    return (BoolVal(v is None), IntVal(v if v is not None else 0))


def _station_cells(s: SnapshotStation) -> dict[tuple[str, str], Cell]:
    return {
        ("stations", "id"): _const_cell(s.id),
        ("stations", "threshold"): _const_cell(s.threshold),
    }


def _reading_cells(
    r: SnapshotReading, row_idx: int, fill_vars: dict[int, object]
) -> dict[tuple[str, str], Cell]:
    if row_idx in fill_vars:
        # 待补测：补测后必有值，取值为区间内符号变量
        value_cell: Cell = (BoolVal(False), fill_vars[row_idx])
    else:
        value_cell = _const_cell(r.value)
    return {
        ("readings", "station_id"): _const_cell(r.station_id),
        ("readings", "value"): value_cell,
    }


def _null_reading_cells() -> dict[tuple[str, str], Cell]:
    return {
        ("readings", "station_id"): (BoolVal(True), IntVal(0)),
        ("readings", "value"): (BoolVal(True), IntVal(0)),
    }


def _where_kept(query: QueryIR, cells: dict[tuple[str, str], Cell]):
    """WHERE 保留条件（三值逻辑：UNKNOWN 被过滤）。"""
    if query.where is None:
        return BoolVal(True)
    defined, val = encode_bool(query.where, cells)
    return ZAnd(defined, val)


def _output_rows(
    stations: list[SnapshotStation],
    readings: list[SnapshotReading],
    fill_vars: dict[int, object],
    query: QueryIR,
) -> list[_OutRow]:
    """把查询在具体快照上展开为 (站点 id, 保留条件) 列表。

    查询已保证以 stations 为 FROM 表且仅投影 stations.id；连接匹配条件中
    涉及待补测 value 时，匹配本身也是符号条件。
    """
    if query.join is None:
        return [(s.id, _where_kept(query, _station_cells(s))) for s in stations]
    join = query.join
    jl, jr = join.left_col, join.right_col
    # 调用方已保证 FROM 表为 stations；不支持自连接，故被连接表必为 readings
    assert jl.table == "stations" and jr.table == "readings"
    rows: list[_OutRow] = []
    for s in stations:
        scells = _station_cells(s)
        n1, v1 = scells[(jl.table, jl.column)]
        matches = []
        for idx, r in enumerate(readings):
            rcells = _reading_cells(r, idx, fill_vars)
            n2, v2 = rcells[(jr.table, jr.column)]
            # SQL 等值连接：任一侧为 NULL 则不匹配
            match = ZAnd(ZNot(n1), ZNot(n2), v1 == v2)
            matches.append((match, {**scells, **rcells}))
        for match, cells in matches:
            rows.append((s.id, ZAnd(match, _where_kept(query, cells))))
        if join.kind == "left":
            # 左表无匹配行时补 NULL 扩展行（匹配与否可为符号条件）
            any_match = ZOr(*[m for m, _ in matches]) if matches else BoolVal(False)
            cells = {**scells, **_null_reading_cells()}
            rows.append((s.id, ZAnd(ZNot(any_match), _where_kept(query, cells))))
    return rows


# ---------- 极值求解 ----------

def _optimize(
    count_expr,
    fill_vars: dict[int, object],
    fill_ranges: dict[int, tuple[int, int]],
    direction: str,
    timeout_ms: int,
) -> tuple[Extremum | None, str]:
    """对行数表达式求最小 / 最大值；返回 (极值与补测见证, 错误说明)。"""
    opt = Optimize()
    opt.set("timeout", timeout_ms)
    for row, var in fill_vars.items():
        lo, hi = fill_ranges[row]
        opt.add(lo <= var, var <= hi)
    handle = opt.minimize(count_expr) if direction == "min" else opt.maximize(count_expr)
    result = opt.check()
    if result == z3_unknown:
        return None, f"求解器无法判定（原因: {opt.reason_unknown()}）"
    if result != sat:
        return None, f"求解器返回异常状态: {result}"
    lower, upper = opt.lower(handle), opt.upper(handle)
    # 超时等情况下可能只得到非最优可行解：要求上下界已收敛到同一整数
    if not (is_int_value(lower) and is_int_value(upper)):
        return None, "求解未收敛到确定最优值"
    if lower.as_long() != upper.as_long():
        return None, "求解超时：未收敛到最优值"
    model = opt.model()
    fills = {
        row: model.eval(var, model_completion=True).as_long()
        for row, var in fill_vars.items()
    }
    return Extremum(count=lower.as_long(), fills=fills), ""
