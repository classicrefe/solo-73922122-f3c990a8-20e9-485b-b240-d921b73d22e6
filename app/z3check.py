"""基于 z3 的有界等价性判定。

在每表 0~MAX_ROWS 行、列值取 PostgreSQL INTEGER 全域（含 NULL）的所有合法
数据库状态下，判定两条查询的结果多重集（按列位置、忽略顺序、保留重复）是否
存在差异。差异条件：存在某个元组值，其在两侧结果中的出现次数不同。
"""
from __future__ import annotations

from dataclasses import dataclass

from z3 import (
    And as ZAnd,
    Bool,
    BoolVal,
    If,
    Implies,
    Int,
    IntVal,
    Not as ZNot,
    Or as ZOr,
    Solver,
    is_true,
    sat,
    unknown as z3_unknown,
    unsat,
)

from .ir import (
    FOREIGN_KEYS,
    INT_MAX,
    INT_MIN,
    MAX_ROWS,
    PRIMARY_KEY,
    SCHEMA,
    And,
    BoolNode,
    Compare,
    IsNull,
    Not,
    Or,
    QueryIR,
)

# 符号单元格：(is_null: z3 Bool, value: z3 Int)
Cell = tuple[object, object]
# 符号行：(存在条件, {(table, column): Cell})
Row = tuple[object, dict[tuple[str, str], "Cell"]]
# 结果多重集元素：(保留条件, 投影元组)
OutRow = tuple[object, tuple["Cell", ...]]


@dataclass
class CounterexampleData:
    """z3 模型提取出的具体数据库状态。"""

    stations: list[tuple[int, int | None]]          # (id, threshold)
    readings: list[tuple[int | None, int | None]]   # (station_id, value)


@dataclass
class Z3Outcome:
    status: str  # 'equivalent' | 'counterexample' | 'unknown'
    witness: CounterexampleData | None = None
    detail: str = ""


class SymbolicDB:
    """stations / readings 各 MAX_ROWS 行的符号化数据库状态及完整性约束。"""

    def __init__(self) -> None:
        self.present: dict[str, list] = {}
        self.cells: dict[str, dict[str, list[Cell]]] = {}
        for tname, cols in SCHEMA.items():
            self.present[tname] = [Bool(f"{tname}_p{i}") for i in range(MAX_ROWS)]
            tcells: dict[str, list[Cell]] = {}
            for col in cols:
                nulls = [Bool(f"{tname}_{col}_n{i}") for i in range(MAX_ROWS)]
                vals = [Int(f"{tname}_{col}_v{i}") for i in range(MAX_ROWS)]
                tcells[col] = list(zip(nulls, vals))
            self.cells[tname] = tcells

    def constraints(self) -> list:
        cs: list = []
        for tname, cols in SCHEMA.items():
            pk = PRIMARY_KEY.get(tname)
            for col in cols:
                for i in range(MAX_ROWS):
                    is_null, val = self.cells[tname][col][i]
                    # int4 值域（NULL 标记与取值解耦，NULL 时取值无关）
                    cs.append(ZAnd(INT_MIN <= val, val <= INT_MAX))
                    if col == pk:
                        cs.append(ZNot(is_null))  # 主键非空
            if pk is not None:
                pkcells = self.cells[tname][pk]
                present = self.present[tname]
                for i in range(MAX_ROWS):
                    for j in range(i + 1, MAX_ROWS):
                        cs.append(
                            Implies(
                                ZAnd(present[i], present[j]),
                                pkcells[i][1] != pkcells[j][1],
                            )
                        )
        for child, fkcol, parent, pkcol in FOREIGN_KEYS:
            for i in range(MAX_ROWS):
                is_null, val = self.cells[child][fkcol][i]
                refs = ZOr(
                    *[
                        ZAnd(
                            self.present[parent][j],
                            self.cells[parent][pkcol][j][1] == val,
                        )
                        for j in range(MAX_ROWS)
                    ]
                )
                cs.append(
                    Implies(ZAnd(self.present[child][i], ZNot(is_null)), refs)
                )
        return cs


def check_bounded_equivalence(
    q1: QueryIR, q2: QueryIR, timeout_ms: int = 10000
) -> Z3Outcome:
    """判定两条查询在界内是否等价；返回等价 / 反例 / 未知。"""
    db = SymbolicDB()
    ms1 = _result_multiset(q1, db)
    ms2 = _result_multiset(q2, db)
    solver = Solver()
    solver.set("timeout", timeout_ms)
    solver.add(*db.constraints())
    solver.add(_multiset_diff(ms1, ms2))
    result = solver.check()
    if result == unsat:
        return Z3Outcome(
            "equivalent",
            detail=f"每表 0~{MAX_ROWS} 行的所有合法取值下结果多重集相同",
        )
    if result == z3_unknown:
        return Z3Outcome(
            "unknown", detail=f"求解器无法判定（原因: {solver.reason_unknown()}）"
        )
    if result != sat:  # pragma: no cover - 防御
        return Z3Outcome("unknown", detail=f"求解器返回异常状态: {result}")
    return Z3Outcome("counterexample", witness=_extract(solver.model(), db))


# ---------- 查询编码 ----------

def _row_cells(db: SymbolicDB, tname: str, i: int) -> dict[tuple[str, str], Cell]:
    return {(tname, c): db.cells[tname][c][i] for c in SCHEMA[tname]}


def _null_cells(tname: str) -> dict[tuple[str, str], Cell]:
    return {(tname, c): (BoolVal(True), IntVal(0)) for c in SCHEMA[tname]}


def _base_rows(q: QueryIR, db: SymbolicDB) -> list[Row]:
    """FROM/JOIN 展开为符号行列表（含存在条件）。"""
    t1 = q.tables[0]
    if q.join is None:
        return [
            (db.present[t1][i], _row_cells(db, t1, i)) for i in range(MAX_ROWS)
        ]
    t2 = q.tables[1]
    jc1, jc2 = q.join.left_col, q.join.right_col

    def join_match(i: int, j: int):
        n1, v1 = db.cells[t1][jc1.column][i]
        n2, v2 = db.cells[t2][jc2.column][j]
        # SQL 等值连接：任一侧为 NULL 则不匹配
        return ZAnd(ZNot(n1), ZNot(n2), v1 == v2)

    rows: list[Row] = []
    for i in range(MAX_ROWS):
        for j in range(MAX_ROWS):
            present = ZAnd(db.present[t1][i], db.present[t2][j], join_match(i, j))
            cells = {**_row_cells(db, t1, i), **_row_cells(db, t2, j)}
            rows.append((present, cells))
    if q.join.kind == "left":
        # 左表无匹配行时补 NULL 扩展行
        for i in range(MAX_ROWS):
            any_match = ZOr(
                *[
                    ZAnd(db.present[t2][j], join_match(i, j))
                    for j in range(MAX_ROWS)
                ]
            )
            present = ZAnd(db.present[t1][i], ZNot(any_match))
            cells = {**_row_cells(db, t1, i), **_null_cells(t2)}
            rows.append((present, cells))
    return rows


def _encode_bool(node: BoolNode, cells: dict[tuple[str, str], Cell]):
    """SQL 三值逻辑编码，返回 (defined, value)。

    TRUE=(True,True)，FALSE=(True,False)，UNKNOWN=(False,*)。
    """
    if isinstance(node, Compare):
        is_null, val = cells[(node.col.table, node.col.column)]
        cmp_expr = _apply_cmp(node.op, val, node.value)
        return ZNot(is_null), cmp_expr
    if isinstance(node, IsNull):
        is_null, _ = cells[(node.col.table, node.col.column)]
        return BoolVal(True), ZNot(is_null) if node.negated else is_null
    if isinstance(node, And):
        da, va = _encode_bool(node.left, cells)
        db_, vb = _encode_bool(node.right, cells)
        defined = ZOr(ZAnd(da, db_), ZAnd(da, ZNot(va)), ZAnd(db_, ZNot(vb)))
        return defined, ZAnd(va, vb)
    if isinstance(node, Or):
        da, va = _encode_bool(node.left, cells)
        db_, vb = _encode_bool(node.right, cells)
        defined = ZOr(ZAnd(da, db_), ZAnd(da, va), ZAnd(db_, vb))
        return defined, ZOr(va, vb)
    if isinstance(node, Not):
        d, v = _encode_bool(node.child, cells)
        return d, ZNot(v)
    raise TypeError(f"未知布尔节点: {node!r}")


def _apply_cmp(op: str, val, const: int):
    if op == "=":
        return val == const
    if op == "!=":
        return val != const
    if op == "<":
        return val < const
    if op == "<=":
        return val <= const
    if op == ">":
        return val > const
    if op == ">=":
        return val >= const
    raise ValueError(op)  # pragma: no cover


def _result_multiset(q: QueryIR, db: SymbolicDB) -> list[OutRow]:
    out: list[OutRow] = []
    for present, cells in _base_rows(q, db):
        if q.where is not None:
            defined, val = _encode_bool(q.where, cells)
            kept = ZAnd(present, defined, val)
        else:
            kept = present
        proj = tuple(cells[(c.table, c.column)] for c in q.projections)
        out.append((kept, proj))
    return out


# ---------- 多重集差异编码 ----------

def _cell_eq(c1: Cell, c2: Cell):
    # 结果比较按值语义：NULL 与 NULL 视为相同
    return ZAnd(c1[0] == c2[0], ZOr(c1[0], c1[1] == c2[1]))


def _tuple_eq(t1: tuple[Cell, ...], t2: tuple[Cell, ...]):
    return ZAnd(*[_cell_eq(a, b) for a, b in zip(t1, t2)])


def _count(ms: list[OutRow], tup: tuple[Cell, ...]):
    terms = [If(ZAnd(kept, _tuple_eq(row, tup)), 1, 0) for kept, row in ms]
    total = 0
    for t in terms:
        total = total + t
    return total


def _multiset_diff(ms1: list[OutRow], ms2: list[OutRow]):
    """存在某个出现的元组值，两侧计数不同。"""
    clauses = []
    for kept, tup in ms1:
        clauses.append(ZAnd(kept, _count(ms1, tup) != _count(ms2, tup)))
    for kept, tup in ms2:
        clauses.append(ZAnd(kept, _count(ms1, tup) != _count(ms2, tup)))
    return ZOr(*clauses) if clauses else BoolVal(False)


# ---------- 模型提取 ----------

def _extract(model, db: SymbolicDB) -> CounterexampleData:
    def ev_bool(b) -> bool:
        return is_true(model.eval(b, model_completion=True))

    def ev_cell(cell: Cell):
        is_null, val = cell
        if ev_bool(is_null):
            return None
        return model.eval(val, model_completion=True).as_long()

    stations: list[tuple[int, int | None]] = []
    for i in range(MAX_ROWS):
        if ev_bool(db.present["stations"][i]):
            stations.append(
                (
                    model.eval(
                        db.cells["stations"]["id"][i][1], model_completion=True
                    ).as_long(),
                    ev_cell(db.cells["stations"]["threshold"][i]),
                )
            )
    readings: list[tuple[int | None, int | None]] = []
    for i in range(MAX_ROWS):
        if ev_bool(db.present["readings"][i]):
            readings.append(
                (
                    ev_cell(db.cells["readings"]["station_id"][i]),
                    ev_cell(db.cells["readings"]["value"][i]),
                )
            )
    return CounterexampleData(stations=stations, readings=readings)
