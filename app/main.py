"""FastAPI 入口：SQL 改写有界等价核对 API。"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException

from .bounds import (
    BoundsOutcome,
    SnapshotReading,
    SnapshotStation,
    compute_station_bounds,
)
from .ir import MAX_ROWS
from .models import (
    AlertBoundsRequest,
    AlertBoundsResponse,
    CheckRequest,
    CheckResponse,
    Counterexample,
    ExtremumInfo,
    FillAssignment,
    StationAlertBounds,
)
from .sql_parser import SqlRejected, parse_query
from .verifier import Verifier, setup_sql
from .z3check import check_bounded_equivalence

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"

app = FastAPI(
    title="SQL 改写有界等价核对 API",
    description=(
        "面向数仓质控 SQL 改写的等价性核对：提交原始 SELECT 与改写 SELECT，"
        "在固定模式 stations(id 主键, threshold) / readings(station_id 外键, value)"
        "（全列 integer，非主键可 NULL）下，枚举每表 0~3 行的所有合法取值，"
        "按列位置比较结果多重集（忽略顺序、保留重复）。"
        "差异会在隔离 PostgreSQL 中实际复现后才报 NOT_EQUIVALENT；"
        "界内无反例报 EQUIVALENT；求解或复现失败报 UNKNOWN。"
    ),
    version="1.0.0",
)

_QUERY_EXAMPLES = {
    "equivalent-integer-rewrite": {
        "summary": "界内等价：整数域上 > 0 与 >= 1 同义",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations WHERE threshold >= 1",
        },
    },
    "not-equivalent-null-semantics": {
        "summary": "不等价：OR IS NULL 改变了 NULL 行的去留",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold = 5",
            "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
        },
    },
    "not-equivalent-left-vs-inner-join": {
        "summary": "不等价：LEFT JOIN 与 INNER JOIN（无读数站点）",
        "value": {
            "query_a": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id"
            ),
            "query_b": (
                "SELECT s.id FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id"
            ),
        },
    },
    "equivalent-left-join-counting": {
        "summary": "界内等价：连接后过滤的两种写法",
        "value": {
            "query_a": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 10 AND s.threshold IS NOT NULL"
            ),
            "query_b": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE NOT (r.value < 10) AND NOT (s.threshold IS NULL)"
            ),
        },
    },
}


def _dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def _solver_timeout_ms(req: CheckRequest | AlertBoundsRequest) -> int:
    if req.solver_timeout_ms is not None:
        return req.solver_timeout_ms
    return int(os.environ.get("SOLVER_TIMEOUT_MS", "10000"))


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "sql-equivalence-checker",
        "docs": "/docs",
        "check_endpoint": "POST /check",
        "alert_bounds_endpoint": "POST /alert-bounds",
        "bound_rows_per_table": MAX_ROWS,
    }


@app.get("/health", summary="健康检查（含数据库连通性）")
def health() -> dict:
    db_up = Verifier(_dsn()).ping()
    return {"status": "ok", "database": "up" if db_up else "down"}


@app.post(
    "/check",
    response_model=CheckResponse,
    summary="核对两条 SELECT 在界内是否等价",
)
def check_equivalence(
    req: CheckRequest = Body(openapi_examples=_QUERY_EXAMPLES),
) -> CheckResponse:
    # 1. 解析与白名单校验
    try:
        ir_a = parse_query(req.query_a)
        ir_b = parse_query(req.query_b)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    if len(ir_a.projections) != len(ir_b.projections):
        raise HTTPException(
            status_code=400,
            detail="两条查询的投影列数不一致，无法按列位置比较",
        )

    # 2. z3 有界等价判定
    outcome = check_bounded_equivalence(ir_a, ir_b, _solver_timeout_ms(req))
    if outcome.status == "equivalent":
        return CheckResponse(
            verdict="EQUIVALENT",
            message=(
                f"在每表 0~{MAX_ROWS} 行的所有合法取值下，两侧结果多重集相同"
                "（有界等价，不构成更大表上的证明）。"
            ),
        )
    if outcome.status == "unknown":
        return CheckResponse(
            verdict="UNKNOWN", message=f"求解失败：{outcome.detail}"
        )

    # 3. 候选反例必须在隔离 PostgreSQL 中实际复现
    assert outcome.witness is not None
    repro = Verifier(_dsn()).reproduce(outcome.witness, req.query_a, req.query_b)
    if not repro.ok:
        return CheckResponse(verdict="UNKNOWN", message=repro.detail)
    if not repro.differs:
        return CheckResponse(
            verdict="UNKNOWN",
            message="内部不一致：z3 反例在 PostgreSQL 中未复现差异，按未知处理。",
        )
    return CheckResponse(
        verdict="NOT_EQUIVALENT",
        message="两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
        counterexample=Counterexample(
            setup_sql=setup_sql(outcome.witness),
            query_a_result=repro.query_a_result or [],
            query_b_result=repro.query_b_result or [],
        ),
    )


# ---------- 待补测读数影响分析 ----------

_ALERT_EXAMPLES = {
    "pending-when-fill-high": {
        "summary": "待补测：站 1 补测值偏高时告警达阈值",
        "value": {
            "stations": [
                {"id": 1, "threshold": 5},
                {"id": 2, "threshold": 5},
            ],
            "readings": [
                {"station_id": 1, "value": 7},
                {"station_id": 1, "value": None},
                {"station_id": 2, "value": 3},
            ],
            "fill_ranges": [{"row": 1, "min": 0, "max": 10}],
            "alert_threshold": 2,
            "query": (
                "SELECT s.id FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 5"
            ),
        },
    },
    "left-join-null-extension": {
        "summary": "LEFT JOIN：补测值决定匹配行去留",
        "value": {
            "stations": [{"id": 1, "threshold": None}],
            "readings": [{"station_id": 1, "value": None}],
            "fill_ranges": [{"row": 0, "min": 0, "max": 10}],
            "alert_threshold": 1,
            "query": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 5 OR r.value IS NULL"
            ),
        },
    },
    "no-pending-readings": {
        "summary": "无待补测读数：上下界相同",
        "value": {
            "stations": [{"id": 1, "threshold": 9}],
            "readings": [
                {"station_id": 1, "value": 1},
                {"station_id": 1, "value": 2},
            ],
            "fill_ranges": [],
            "alert_threshold": 2,
            "query": (
                "SELECT s.id FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE r.value > 0"
            ),
        },
    },
}


def _validate_snapshot(req: AlertBoundsRequest) -> dict[int, tuple[int, int]]:
    """校验快照完整性约束（沿用固定模式），返回 {readings 行号: (下界, 上界)}。"""
    ids = [s.id for s in req.stations]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="stations.id 主键重复")
    idset = set(ids)
    for i, r in enumerate(req.readings):
        if r.station_id is not None and r.station_id not in idset:
            raise HTTPException(
                status_code=400,
                detail=f"readings 第 {i} 行 station_id={r.station_id} "
                "外键悬空（stations 中不存在该 id）",
            )
    null_rows = {i for i, r in enumerate(req.readings) if r.value is None}
    fill_ranges: dict[int, tuple[int, int]] = {}
    for fr in req.fill_ranges:
        if fr.row >= len(req.readings):
            raise HTTPException(
                status_code=400, detail=f"fill_ranges 行号越界: {fr.row}"
            )
        if fr.row in fill_ranges:
            raise HTTPException(
                status_code=400, detail=f"fill_ranges 行号重复: {fr.row}"
            )
        if req.readings[fr.row].value is not None:
            raise HTTPException(
                status_code=400,
                detail=f"readings 第 {fr.row} 行 value 非 NULL，无需指定补测区间",
            )
        if fr.min > fr.max:
            raise HTTPException(
                status_code=400,
                detail=f"fill_ranges 第 {fr.row} 行区间下界大于上界",
            )
        fill_ranges[fr.row] = (fr.min, fr.max)
    missing = sorted(null_rows - set(fill_ranges))
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"readings 第 {missing} 行 value 为 NULL（待补测），"
            "必须在 fill_ranges 中指定补测区间",
        )
    return fill_ranges


def _apply_fills(
    readings: list[tuple], fills: dict[int, int]
) -> list[tuple]:
    """把一组补测值应用到 readings 快照（NULL value 行由 fills 覆盖）。"""
    return [
        (sid, fills[i] if v is None else v)
        for i, (sid, v) in enumerate(readings)
    ]


@app.post(
    "/alert-bounds",
    response_model=AlertBoundsResponse,
    summary="待补测读数下每站告警行数上下界",
)
def alert_bounds(
    req: AlertBoundsRequest = Body(openapi_examples=_ALERT_EXAMPLES),
) -> AlertBoundsResponse:
    # 1. 快照完整性校验（沿用固定模式约束）
    fill_ranges = _validate_snapshot(req)

    # 2. 查询解析与形态校验：FROM stations、仅投影 stations.id
    try:
        ir = parse_query(req.query)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    if ir.tables[0] != "stations":
        raise HTTPException(
            status_code=400, detail="查询必须以 stations 为 FROM 表"
        )
    if [(c.table, c.column) for c in ir.projections] != [("stations", "id")]:
        raise HTTPException(
            status_code=400, detail="查询只能投影 stations.id 一列"
        )

    # 3. z3 极值求解：每站结果行数在所有合法补测取值下的最小 / 最大值
    outcome: BoundsOutcome = compute_station_bounds(
        [SnapshotStation(s.id, s.threshold) for s in req.stations],
        [SnapshotReading(r.station_id, r.value) for r in req.readings],
        fill_ranges,
        ir,
        _solver_timeout_ms(req),
    )
    if outcome.status != "ok":
        return AlertBoundsResponse(
            status="UNKNOWN",
            alert_threshold=req.alert_threshold,
            message=f"求解失败：{outcome.detail}",
        )

    # 4. 每个极值的补测见证都须在隔离 PostgreSQL 中重跑确认行数
    verifier = Verifier(_dsn())
    stations_data = [(s.id, s.threshold) for s in req.stations]
    base_readings = [(r.station_id, r.value) for r in req.readings]
    confirmed: dict[tuple, dict[int, int]] = {}  # 补测见证 -> 每站实际行数
    results: list[StationAlertBounds] = []
    for sb in outcome.stations:
        extrema: list[ExtremumInfo] = []
        for ext in (sb.minimum, sb.maximum):
            key = tuple(sorted(ext.fills.items()))
            if key not in confirmed:
                run = verifier.run_query(
                    stations_data, _apply_fills(base_readings, ext.fills), req.query
                )
                if not run.ok:
                    return AlertBoundsResponse(
                        status="UNKNOWN",
                        alert_threshold=req.alert_threshold,
                        message=run.detail,
                    )
                counts: dict[int, int] = {}
                for row in run.rows or []:
                    counts[row[0]] = counts.get(row[0], 0) + 1
                confirmed[key] = counts
            actual = confirmed[key].get(sb.station_id, 0)
            if actual != ext.count:
                return AlertBoundsResponse(
                    status="UNKNOWN",
                    alert_threshold=req.alert_threshold,
                    message=(
                        f"内部不一致：站点 {sb.station_id} 的极值 {ext.count} 在 "
                        f"PostgreSQL 复算中实际为 {actual} 行，按未知处理。"
                    ),
                )
            extrema.append(
                ExtremumInfo(
                    count=ext.count,
                    fills=[
                        FillAssignment(row=row, value=val)
                        for row, val in sorted(ext.fills.items())
                    ],
                )
            )
        results.append(
            StationAlertBounds(
                station_id=sb.station_id,
                min_count=sb.minimum.count,
                max_count=sb.maximum.count,
                pending=sb.minimum.count < req.alert_threshold <= sb.maximum.count,
                min_extremum=extrema[0],
                max_extremum=extrema[1],
            )
        )
    pending_n = sum(1 for r in results if r.pending)
    return AlertBoundsResponse(
        status="OK",
        alert_threshold=req.alert_threshold,
        stations=results,
        message=(
            f"共 {len(results)} 站，{pending_n} 站待补测"
            "（最少行数未达阈值、最多行数达到阈值）；"
            "所有极值已在隔离 PostgreSQL 中复算确认。"
        ),
    )
