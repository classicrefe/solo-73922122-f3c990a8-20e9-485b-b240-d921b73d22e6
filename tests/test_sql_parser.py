"""解析器白名单测试：合法子集被接受，越界语法被拒绝。"""
import pytest

from app.ir import And, Compare, IsNull, Not, Or
from app.sql_parser import SqlRejected, parse_query


def test_single_table_projection_and_where():
    q = parse_query("SELECT id, threshold FROM stations WHERE threshold >= -5")
    assert q.tables == ("stations",)
    assert q.join is None
    assert [c.column for c in q.projections] == ["id", "threshold"]
    assert isinstance(q.where, Compare)
    assert q.where.op == ">=" and q.where.value == -5


def test_alias_and_qualified_columns():
    q = parse_query(
        "SELECT s.id AS sid, r.value v FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    assert q.tables == ("stations", "readings")
    assert q.join.kind == "left"
    assert q.join.left_col.table == "stations" and q.join.left_col.column == "id"
    assert q.join.right_col.table == "readings" and q.join.right_col.column == "station_id"
    assert [(c.table, c.column) for c in q.projections] == [
        ("stations", "id"),
        ("readings", "value"),
    ]


def test_inner_join_keyword_variants():
    for kw in ("JOIN", "INNER JOIN"):
        q = parse_query(
            f"SELECT id FROM stations s {kw} readings r ON s.id = r.station_id"
        )
        assert q.join.kind == "inner"
    q = parse_query(
        "SELECT id FROM stations s LEFT OUTER JOIN readings r ON s.id = r.station_id"
    )
    assert q.join.kind == "left"


def test_where_logic_tree_and_is_null():
    q = parse_query(
        "SELECT id FROM stations "
        "WHERE NOT (threshold < 0 OR threshold IS NULL) AND id != 7"
    )
    assert isinstance(q.where, And)
    assert isinstance(q.where.left, Not)
    assert isinstance(q.where.left.child, Or)
    assert isinstance(q.where.left.child.right, IsNull)
    assert q.where.right.op == "!=" and q.where.right.value == 7


def test_is_not_null():
    q = parse_query("SELECT id FROM stations WHERE threshold IS NOT NULL")
    assert isinstance(q.where, IsNull) and q.where.negated


def test_literal_on_left_flips_operator():
    q = parse_query("SELECT id FROM stations WHERE 10 > threshold")
    assert q.where.op == "<" and q.where.value == 10


def test_case_insensitive_unquoted_identifiers():
    q = parse_query("SELECT ID FROM Stations WHERE THRESHOLD = 1")
    assert q.tables == ("stations",)
    assert q.projections[0].column == "id"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM stations",
        "SELECT s.* FROM stations s",
        "SELECT id, COUNT(*) FROM stations GROUP BY id",
        "SELECT id FROM stations ORDER BY id",
        "SELECT id FROM stations LIMIT 1",
        "SELECT DISTINCT id FROM stations",
        "SELECT id FROM stations HAVING id > 1",
        "SELECT id + 1 FROM stations",
        "SELECT abs(id) FROM stations",
        "SELECT 1 FROM stations",
        "SELECT id FROM stations WHERE id IN (1, 2)",
        "SELECT id FROM stations WHERE id BETWEEN 1 AND 2",
        "SELECT id FROM stations WHERE id = (SELECT id FROM stations)",
        "SELECT id FROM stations WHERE threshold = 5.5",
        "SELECT id FROM stations WHERE threshold = '5'",
        "SELECT id FROM stations WHERE id = threshold",
        "SELECT id FROM stations WHERE 1 = 1",
        "SELECT id FROM stations WHERE id > 1 LIKE id > 2",
        "SELECT s.id FROM stations s RIGHT JOIN readings r ON s.id = r.station_id",
        "SELECT s.id FROM stations s FULL JOIN readings r ON s.id = r.station_id",
        "SELECT s.id FROM stations s CROSS JOIN readings r",
        "SELECT s.id FROM stations s, readings r",
        "SELECT s.id FROM stations s JOIN readings r ON s.id = r.station_id "
        "JOIN stations t ON t.id = s.id",
        "SELECT s.id FROM stations s JOIN readings r ON s.id = r.station_id AND r.value > 1",
        "SELECT s.id FROM stations s JOIN readings r ON s.id > r.station_id",
        "SELECT s.id FROM stations s JOIN readings r USING (id)",
        "SELECT a.id FROM stations a JOIN stations b ON a.id = b.id",
        "SELECT id FROM stations UNION SELECT id FROM stations",
        "WITH x AS (SELECT id FROM stations) SELECT id FROM x",
        "SELECT id FROM (SELECT id FROM stations) t",
        "SELECT id FROM other_table",
        "SELECT id, nope FROM stations",
        "SELECT id FROM stations WHERE nope = 1",
        "SELECT x.id FROM stations",
        "UPDATE stations SET threshold = 1",
        "DELETE FROM stations",
        "SELECT id FROM stations; SELECT id FROM stations",
        "SELECT id FROM public.stations",
        "",
        "SELECT id FROM",
    ],
)
def test_rejected_syntax(sql):
    with pytest.raises(SqlRejected):
        parse_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT value FROM readings WHERE value IS NULL OR value IS NOT NULL",
        "SELECT s.id, s.threshold, r.station_id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE (r.value <= 100 AND NOT (s.threshold = 0)) OR r.value IS NULL",
        "SELECT id FROM stations WHERE threshold > -2147483648",
        "SELECT id FROM stations WHERE 5 = threshold",
        "SELECT station_id FROM readings",
    ],
)
def test_accepted_syntax(sql):
    parse_query(sql)
