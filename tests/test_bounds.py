"""每站告警行数上下界的 z3 求解测试（不依赖数据库）。"""
import pytest

from app.bounds import (
    SnapshotReading,
    SnapshotStation,
    compute_station_bounds,
)
from app.sql_parser import parse_query


def run(stations, readings, fills, sql, timeout_ms=20000):
    return compute_station_bounds(
        stations, readings, fills, parse_query(sql), timeout_ms
    )


def bounds_of(outcome, station_id):
    assert outcome.status == "ok", outcome.detail
    for sb in outcome.stations:
        if sb.station_id == station_id:
            return sb
    raise AssertionError(f"站点 {station_id} 不在结果中")


# ---------- INNER JOIN：补测值影响 WHERE 去留 ----------


def test_inner_join_fill_controls_where():
    outcome = run(
        [SnapshotStation(1, 5), SnapshotStation(2, 5)],
        [
            SnapshotReading(1, 7),
            SnapshotReading(1, None),
            SnapshotReading(2, 3),
        ],
        {1: (0, 10)},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id WHERE r.value >= 5",
    )
    b1 = bounds_of(outcome, 1)
    # 读数 7 恒保留；待补测行 fill>=5 时才保留
    assert (b1.minimum.count, b1.maximum.count) == (1, 2)
    assert 0 <= b1.minimum.fills[1] <= 4
    assert 5 <= b1.maximum.fills[1] <= 10
    b2 = bounds_of(outcome, 2)
    assert (b2.minimum.count, b2.maximum.count) == (0, 0)
    # 每个极值都附完整补测值（覆盖全部待补测行）
    assert set(b2.minimum.fills) == {1}
    assert 0 <= b2.minimum.fills[1] <= 10


def test_duplicates_counted():
    outcome = run(
        [SnapshotStation(1, 0)],
        [SnapshotReading(1, 1), SnapshotReading(1, 2), SnapshotReading(1, 3)],
        {},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (3, 3)
    assert b.minimum.fills == {} and b.maximum.fills == {}


def test_null_fk_never_matches():
    outcome = run(
        [SnapshotStation(1, 0)],
        [SnapshotReading(None, None)],
        {0: (0, 10)},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (0, 0)


# ---------- 连接条件涉及待补测 value：匹配本身不确定 ----------


def test_symbolic_join_match_on_value():
    outcome = run(
        [SnapshotStation(1, 5)],
        [SnapshotReading(1, None)],
        {0: (0, 10)},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.threshold = r.value",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (0, 1)
    assert b.maximum.fills[0] == 5
    assert b.minimum.fills[0] != 5


def test_left_join_extension_row_symbolic_match():
    # 连接列为待补测 value：fill=5 时有匹配行，否则落到 NULL 扩展行
    outcome = run(
        [SnapshotStation(1, 5)],
        [SnapshotReading(1, None)],
        {0: (0, 10)},
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.threshold = r.value",
    )
    b = bounds_of(outcome, 1)
    # 两种情况下都恰好输出一行（匹配行或扩展行）
    assert (b.minimum.count, b.maximum.count) == (1, 1)


# ---------- LEFT JOIN ----------


def test_left_join_extension_row_kept():
    outcome = run(
        [SnapshotStation(1, None)],
        [],
        {},
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.id = r.station_id",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (1, 1)


def test_left_join_fill_controls_match_row():
    outcome = run(
        [SnapshotStation(1, None)],
        [SnapshotReading(1, None)],
        {0: (0, 10)},
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.id = r.station_id WHERE r.value >= 5 OR r.value IS NULL",
    )
    b = bounds_of(outcome, 1)
    # fill>=5 时匹配行保留；fill<5 时匹配行被过滤（无扩展行，因匹配存在）
    assert (b.minimum.count, b.maximum.count) == (0, 1)
    assert 0 <= b.minimum.fills[0] <= 4
    assert 5 <= b.maximum.fills[0] <= 10


# ---------- 单表查询与 stations 列筛选 ----------


def test_single_table_where_on_threshold():
    outcome = run(
        [SnapshotStation(1, 5), SnapshotStation(2, None), SnapshotStation(3, 7)],
        [],
        {},
        "SELECT id FROM stations WHERE threshold >= 5",
    )
    assert bounds_of(outcome, 1).maximum.count == 1
    # NULL threshold：三值逻辑 UNKNOWN 被过滤
    assert bounds_of(outcome, 2).maximum.count == 0
    assert bounds_of(outcome, 3).maximum.count == 1


def test_fill_range_boundary_values():
    # 区间端点恰好决定 WHERE 去留
    outcome = run(
        [SnapshotStation(1, 0)],
        [SnapshotReading(1, None)],
        {0: (5, 5)},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id WHERE r.value = 5",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (1, 1)
    assert b.minimum.fills[0] == 5


def test_empty_stations_snapshot():
    outcome = run(
        [],
        [],
        {},
        "SELECT id FROM stations WHERE threshold >= 5",
    )
    assert outcome.status == "ok"
    assert outcome.stations == []


def test_int4_extreme_fill_range():
    outcome = run(
        [SnapshotStation(1, 0)],
        [SnapshotReading(1, None)],
        {0: (-(2**31), 2**31 - 1)},
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id WHERE r.value > 0",
    )
    b = bounds_of(outcome, 1)
    assert (b.minimum.count, b.maximum.count) == (0, 1)
    assert b.maximum.fills[0] > 0
    assert b.minimum.fills[0] <= 0
