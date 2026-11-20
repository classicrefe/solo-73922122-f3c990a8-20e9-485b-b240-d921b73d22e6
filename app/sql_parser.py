"""SQL 解析与白名单校验：仅允许受限 SELECT 子集，其余语法一律拒绝。

支持的语法子集：
  - SELECT 列表：仅列投影（可带表限定与别名，忽略别名，按位置比较）
  - FROM：单表，或两表间至多一次 INNER / LEFT 等值连接（ON a.col = b.col）
  - WHERE：列与整数字面量比较（= != < <= > >=）、IS [NOT] NULL、AND / OR / NOT、括号
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .ir import (
    SCHEMA,
    And,
    BoolNode,
    ColRef,
    Compare,
    IsNull,
    JoinSpec,
    Not,
    Or,
    QueryIR,
)


class SqlRejected(ValueError):
    """输入 SQL 超出受支持的语法子集。"""


def _reject(reason: str) -> None:
    raise SqlRejected(reason)


_CMP_NODES: dict[type, str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
# 字面量在左、列在右时的运算符翻转
_FLIP = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_INT_RE = re.compile(r"[0-9]+")

# SELECT 顶层禁止出现的子句（args 键存在且非 None 即拒绝）
_FORBIDDEN_SELECT_ARGS = (
    "with", "distinct", "group", "having", "qualify", "order", "limit",
    "offset", "windows", "lock", "locks", "kind", "operation_modifiers",
    "hint", "exclude", "settings", "sample", "pivots", "connect", "match",
    "into",
)

# 全树禁止出现的节点类型（白名单之外的纵深防御）
_FORBIDDEN_NODE_NAMES = (
    "Subquery", "UDTF", "Func", "AggFunc", "Star", "Case", "Cast", "TryCast",
    "In", "Between", "Like", "ILike", "SimilarTo", "RegexpLike", "RegexpILike",
    "Exists", "Any", "All", "Union", "Except", "Intersect", "CTE", "Window",
    "Lateral", "Unnest", "Pivot", "Interval", "Extract", "Array", "Tuple",
    "Slice", "Parameter", "Placeholder", "Command", "SetOperation", "Values",
    "Merge", "Insert", "Update", "Delete", "Create", "Drop", "Alter",
)
_FORBIDDEN_NODES: tuple[type, ...] = tuple(
    t
    for t in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES)
    if isinstance(t, type)
)

# 白名单节点：sqlglot 中 And/Or/Not/Neg 等也继承自 exp.Func，
# 命中禁用类型时若为显式白名单节点则放行。
_ALLOWED_NODES: tuple[type, ...] = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in (
            "Select", "From", "Where", "Table", "TableAlias", "Join", "Column",
            "Identifier", "Alias", "Paren", "And", "Or", "Not", "Neg", "Is",
            "Null", "Literal", "EQ", "NEQ", "LT", "LTE", "GT", "GTE",
        )
    )
    if isinstance(t, type)
)


def parse_query(sql: str) -> QueryIR:
    """将一条受限 SELECT 解析为 QueryIR；越界语法抛 SqlRejected。"""
    sql = (sql or "").strip()
    if not sql:
        _reject("空 SQL")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        _reject(f"SQL 语法错误: {exc}")
    if len(statements) != 1:
        _reject("只允许单条 SELECT 语句")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        _reject("只允许 SELECT 查询")
    _check_select_clauses(tree)
    _check_forbidden_nodes(tree)
    scope, tables = _parse_from(tree)
    join = _parse_join(tree, scope, tables)
    projections = _parse_projections(tree, scope, tables)
    where = _parse_where(tree, scope, tables)
    return QueryIR(
        tables=tuple(tables),
        join=join,
        projections=tuple(projections),
        where=where,
    )


# ---------- 内部实现 ----------

def _check_select_clauses(sel: exp.Select) -> None:
    for key in _FORBIDDEN_SELECT_ARGS:
        if sel.args.get(key) is not None:
            _reject(f"不支持的 SELECT 子句: {key}")


def _check_forbidden_nodes(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES) and not isinstance(
            node, _ALLOWED_NODES
        ):
            _reject(f"不支持的语法节点: {node.__class__.__name__}")


def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _ident_name(node: exp.Expression) -> str:
    """标识符归一化：未加引号的标识符按 SQL 规则折叠为小写。"""
    if not isinstance(node, exp.Identifier):
        _reject("非法标识符")
    raw = node.name
    return raw if node.args.get("quoted") else raw.lower()


def _table_name_and_alias(table: exp.Table) -> tuple[str, str]:
    if table.args.get("db") or table.args.get("catalog"):
        _reject("不允许使用库/模式限定的表名")
    name = _ident_name(table.this)
    if name not in SCHEMA:
        _reject(f"未知表（仅支持 stations / readings）: {name}")
    alias_expr = table.args.get("alias")
    if alias_expr is not None:
        key = _ident_name(alias_expr.this)
    else:
        key = name
    return name, key


def _parse_from(sel: exp.Select) -> tuple[dict[str, str], list[str]]:
    from_expr = sel.args.get("from_") or sel.args.get("from")
    if from_expr is None or not isinstance(from_expr, exp.From):
        _reject("缺少 FROM 子句")
    first = from_expr.this
    if not isinstance(first, exp.Table):
        _reject("FROM 仅支持基表（不支持子查询/函数/VALUES）")
    tables: list[exp.Table] = [first]
    joins = sel.args.get("joins") or []
    if len(joins) > 1:
        _reject("至多允许一次两表连接")
    if joins:
        second = joins[0].this
        if not isinstance(second, exp.Table):
            _reject("JOIN 仅支持基表")
        tables.append(second)

    scope: dict[str, str] = {}
    real_tables: list[str] = []
    for t in tables:
        name, key = _table_name_and_alias(t)
        if name in real_tables:
            _reject("不支持自连接")
        if key in scope:
            _reject(f"表别名重复: {key}")
        scope[key] = name
        real_tables.append(name)
    return scope, real_tables


def _parse_join(
    sel: exp.Select, scope: dict[str, str], tables: list[str]
) -> JoinSpec | None:
    joins = sel.args.get("joins") or []
    if not joins:
        return None
    j = joins[0]
    if j.args.get("using") is not None:
        _reject("不支持 USING，请使用 ON 等值条件")
    if j.args.get("method") is not None:
        _reject("不支持的 JOIN 方式")
    side = str(j.args.get("side") or "").upper()
    kind = str(j.args.get("kind") or "").upper()
    if side == "" and kind in ("", "INNER"):
        jkind = "inner"
    elif side == "LEFT" and kind in ("", "OUTER"):
        jkind = "left"
    else:
        _reject("仅支持 INNER JOIN 与 LEFT [OUTER] JOIN")
    on = j.args.get("on")
    if on is None:
        _reject("JOIN 必须带 ON 等值条件（不支持 CROSS/NATURAL JOIN）")
    on = _unwrap_paren(on)
    if not isinstance(on, exp.EQ):
        _reject("ON 仅允许单个列间等值条件")
    left = _unwrap_paren(on.this)
    right = _unwrap_paren(on.expression)
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        _reject("ON 等值条件两侧都必须是列")
    c1 = _resolve_col(left, scope, tables)
    c2 = _resolve_col(right, scope, tables)
    if c1.table == c2.table:
        _reject("ON 条件必须连接两张不同的表")
    if c1.table == tables[0]:
        left_col, right_col = c1, c2
    else:
        left_col, right_col = c2, c1
    return JoinSpec(kind=jkind, left_col=left_col, right_col=right_col)


def _parse_projections(
    sel: exp.Select, scope: dict[str, str], tables: list[str]
) -> list[ColRef]:
    projections: list[ColRef] = []
    for e in sel.expressions:
        e = _unwrap_paren(e)
        if isinstance(e, exp.Alias):
            e = _unwrap_paren(e.this)
        if isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
            _reject("不支持 t.*，请显式列出列名")
        if not isinstance(e, exp.Column):
            _reject("SELECT 列表仅允许列投影（不支持表达式/函数/字面量/*）")
        projections.append(_resolve_col(e, scope, tables))
    if not projections:
        _reject("SELECT 列表不能为空")
    return projections


def _parse_where(
    sel: exp.Select, scope: dict[str, str], tables: list[str]
) -> BoolNode | None:
    where = sel.args.get("where")
    if where is None:
        return None
    return _parse_bool(where.this, scope, tables)


def _parse_bool(
    node: exp.Expression, scope: dict[str, str], tables: list[str]
) -> BoolNode:
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        return And(
            _parse_bool(node.this, scope, tables),
            _parse_bool(node.expression, scope, tables),
        )
    if isinstance(node, exp.Or):
        return Or(
            _parse_bool(node.this, scope, tables),
            _parse_bool(node.expression, scope, tables),
        )
    if isinstance(node, exp.Not):
        return Not(_parse_bool(node.this, scope, tables))
    if isinstance(node, exp.Is):
        inner = _unwrap_paren(node.this)
        target = _unwrap_paren(node.expression)
        if isinstance(inner, exp.Column) and isinstance(target, exp.Null):
            return IsNull(
                _resolve_col(inner, scope, tables),
                negated=bool(node.args.get("negate")),
            )
        _reject("IS 仅支持 IS NULL / IS NOT NULL")
    for cls, op in _CMP_NODES.items():
        if isinstance(node, cls):
            return _parse_compare(node, op, scope, tables)
    _reject(f"WHERE 不支持的语法: {node.__class__.__name__}")
    raise AssertionError("unreachable")


def _parse_compare(
    node: exp.Expression, op: str, scope: dict[str, str], tables: list[str]
) -> Compare:
    left = _unwrap_paren(node.this)
    right = _unwrap_paren(node.expression)
    if isinstance(left, exp.Column) and not isinstance(right, exp.Column):
        return Compare(_resolve_col(left, scope, tables), op, _int_literal(right))
    if isinstance(right, exp.Column) and not isinstance(left, exp.Column):
        return Compare(
            _resolve_col(right, scope, tables), _FLIP[op], _int_literal(left)
        )
    _reject("比较运算必须一侧为列、另一侧为整数字面量")
    raise AssertionError("unreachable")


def _int_literal(node: exp.Expression) -> int:
    sign = 1
    node = _unwrap_paren(node)
    if isinstance(node, exp.Neg):
        sign = -1
        node = _unwrap_paren(node.this)
    if (
        isinstance(node, exp.Literal)
        and not node.is_string
        and _INT_RE.fullmatch(node.this)
    ):
        return sign * int(node.this)
    _reject("比较运算仅支持整数字面量")
    raise AssertionError("unreachable")


def _resolve_col(
    col: exp.Column, scope: dict[str, str], tables: list[str]
) -> ColRef:
    if col.args.get("db") or col.args.get("catalog"):
        _reject("不允许使用库/模式限定的列名")
    if isinstance(col.this, exp.Star):
        _reject("不支持 *，请显式列出列名")
    name = _ident_name(col.this)
    qual = col.args.get("table")
    if qual is not None:
        q = _ident_name(qual)
        real = scope.get(q)
        if real is None:
            _reject(f"未知表或别名: {q}")
        if name not in SCHEMA[real]:
            _reject(f"表 {real} 不存在列: {name}")
        return ColRef(real, name)
    matches = [t for t in tables if name in SCHEMA[t]]
    if not matches:
        _reject(f"未知列: {name}")
    if len(matches) > 1:
        _reject(f"列名有歧义，请加表限定: {name}")
    return ColRef(matches[0], name)
