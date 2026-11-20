"""z3 有界等价判定测试（不依赖数据库）。"""
import pytest

from app.ir import MAX_ROWS
from app.sql_parser import parse_query
from app.z3check import check_bounded_equivalence


def check(a: str, b: str):
    return check_bounded_equivalence(parse_query(a), parse_query(b), timeout_ms=20000)


def assert_witness_legal(w):
    """反例必须满足行数界、主键唯一、外键引用完整。"""
    assert len(w.stations) <= MAX_ROWS
    assert len(w.readings) <= MAX_ROWS
    ids = [s[0] for s in w.stations]
    assert len(ids) == len(set(ids))
    for sid, _ in w.readings:
        assert sid is None or sid in ids


# ---------- 界内等价 ----------

@pytest.mark.parametrize(
    "a, b",
    [
        # 整数域上 > 0 与 >= 1 同义
        ("SELECT id FROM stations WHERE threshold > 0",
         "SELECT id FROM stations WHERE threshold >= 1"),
        # 三值逻辑：NOT (x = 5) 与 x != 5 都过滤 NULL
        ("SELECT id FROM stations WHERE NOT (threshold = 5)",
         "SELECT id FROM stations WHERE threshold != 5"),
        # 德摩根律（含 NULL 三值语义）
        ("SELECT id FROM stations WHERE NOT (threshold < 5 OR threshold > 10)",
         "SELECT id FROM stations WHERE threshold >= 5 AND threshold <= 10"),
        # IS NULL 恒等式
        ("SELECT id FROM stations WHERE threshold IS NULL OR threshold IS NOT NULL",
         "SELECT id FROM stations"),
        # 别名与显式限定不影响语义
        ("SELECT id FROM stations",
         "SELECT s.id FROM stations s"),
        # 交换律
        ("SELECT id FROM stations WHERE threshold > 1 AND threshold < 9",
         "SELECT id FROM stations WHERE threshold < 9 AND threshold > 1"),
        # 连接结合顺序相同的两种写法
        ("SELECT s.id, r.value FROM stations s INNER JOIN readings r "
         "ON s.id = r.station_id WHERE r.value >= 10",
         "SELECT s.id, r.value FROM stations s INNER JOIN readings r "
         "ON r.station_id = s.id WHERE NOT (r.value < 10)"),
        # 恒假 WHERE 等价于空结果
        ("SELECT id FROM stations WHERE threshold > 5 AND threshold < 5",
         "SELECT id FROM stations WHERE id > 2147483647"),
    ],
)
def test_equivalent_pairs(a, b):
    outcome = check(a, b)
    assert outcome.status == "equivalent", outcome.detail


# ---------- 不等价（应有反例） ----------

@pytest.mark.parametrize(
    "a, b",
    [
        # 过滤条件更宽
        ("SELECT id FROM stations WHERE threshold > 0",
         "SELECT id FROM stations"),
        # NULL 行去留不同
        ("SELECT id FROM stations WHERE threshold = 5",
         "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL"),
        # NOT 与 != 对 NULL 的不同处理（此例 b 保留 NULL）
        ("SELECT id FROM stations WHERE NOT (threshold <= 5)",
         "SELECT id FROM stations WHERE threshold > 5 OR threshold IS NULL"),
        # LEFT JOIN 保留无匹配站点
        ("SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
         "SELECT s.id FROM stations s INNER JOIN readings r ON s.id = r.station_id"),
        # LEFT JOIN 产生重复（一站多条读数）
        ("SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
         "SELECT id FROM stations"),
        # 重复行计数不同（多重集语义）
        ("SELECT station_id FROM readings",
         "SELECT station_id FROM readings WHERE value IS NOT NULL"),
        # 投影列不同
        ("SELECT id, threshold FROM stations",
         "SELECT id, id FROM stations"),
        # 反连接写法与 NOT 不等价（NULL 外键）
        ("SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id "
         "WHERE r.station_id IS NULL",
         "SELECT id FROM stations"),
    ],
)
def test_not_equivalent_pairs(a, b):
    outcome = check(a, b)
    assert outcome.status == "counterexample", outcome.detail
    assert_witness_legal(outcome.witness)


def test_witness_values_within_int4():
    outcome = check(
        "SELECT id FROM stations WHERE threshold > 0",
        "SELECT id FROM stations WHERE threshold >= 0",
    )
    assert outcome.status == "counterexample"
    w = outcome.witness
    assert_witness_legal(w)
    for _, th in w.stations:
        if th is not None:
            assert -(2**31) <= th <= 2**31 - 1
    # threshold = 0 的站点可区分两查询
    assert any(th == 0 for _, th in w.stations if th is not None)
