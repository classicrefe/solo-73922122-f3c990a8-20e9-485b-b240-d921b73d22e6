"""在隔离的 PostgreSQL 模式中实际复现反例。

z3 给出的候选反例必须在真实 PostgreSQL 中重放并确认两侧结果多重集确实不同，
才允许上报"不等价"；复现失败或未复现差异时一律按"未知"处理（由调用方决定）。
隔离方式：每次复现使用独立的临时 SCHEMA，整个流程在单事务内完成并回滚，
不在数据库中留下任何对象。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .z3check import CounterexampleData

_CREATE_STATIONS = "CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)"
_CREATE_READINGS = (
    "CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER)"
)


@dataclass
class ReproOutcome:
    ok: bool                     # 复现流程是否成功执行
    differs: bool = False        # 两侧结果多重集是否确实不同
    detail: str = ""
    query_a_result: list[list[Any]] | None = None
    query_b_result: list[list[Any]] | None = None


def setup_sql(witness: CounterexampleData) -> str:
    """生成可装载的 SQL 脚本（重建表结构与反例数据）。"""
    lines = [
        "DROP TABLE IF EXISTS readings;",
        "DROP TABLE IF EXISTS stations;",
        _CREATE_STATIONS + ";",
        _CREATE_READINGS + ";",
    ]
    if witness.stations:
        vals = ", ".join(f"({sid}, {_lit(th)})" for sid, th in witness.stations)
        lines.append(f"INSERT INTO stations (id, threshold) VALUES {vals};")
    if witness.readings:
        vals = ", ".join(f"({_lit(sid)}, {_lit(v)})" for sid, v in witness.readings)
        lines.append(f"INSERT INTO readings (station_id, value) VALUES {vals};")
    return "\n".join(lines) + "\n"


def _lit(v: Any) -> str:
    return "NULL" if v is None else str(int(v))


def _canon(rows: list[list[Any]]) -> list[tuple]:
    """规范化结果多重集：NULL 可比较、忽略行序、保留重复。"""
    return sorted(
        tuple((0, 0) if v is None else (1, v) for v in row) for row in rows
    )


class Verifier:
    def __init__(self, dsn: str, statement_timeout_ms: int = 10000) -> None:
        self._dsn = dsn
        self._timeout_ms = statement_timeout_ms

    def ping(self) -> bool:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def reproduce(
        self, witness: CounterexampleData, query_a: str, query_b: str
    ) -> ReproOutcome:
        schema = "cc_" + uuid.uuid4().hex
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA "{schema}"')
                    cur.execute(f'SET search_path TO "{schema}"')
                    cur.execute(f"SET LOCAL statement_timeout TO {self._timeout_ms}")
                    cur.execute(_CREATE_STATIONS)
                    cur.execute(_CREATE_READINGS)
                    if witness.stations:
                        cur.executemany(
                            "INSERT INTO stations (id, threshold) VALUES (%s, %s)",
                            witness.stations,
                        )
                    if witness.readings:
                        cur.executemany(
                            "INSERT INTO readings (station_id, value) VALUES (%s, %s)",
                            witness.readings,
                        )
                    cur.execute(query_a)
                    res_a = [list(r) for r in cur.fetchall()]
                    cur.execute(query_b)
                    res_b = [list(r) for r in cur.fetchall()]
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return ReproOutcome(ok=False, detail=f"PostgreSQL 复现失败: {exc}")
        return ReproOutcome(
            ok=True,
            differs=_canon(res_a) != _canon(res_b),
            query_a_result=res_a,
            query_b_result=res_b,
        )
