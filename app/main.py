"""FastAPI 入口：SQL 改写有界等价核对 API。"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException

from .ir import MAX_ROWS
from .models import CheckRequest, CheckResponse, Counterexample
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


def _solver_timeout_ms(req: CheckRequest) -> int:
    if req.solver_timeout_ms is not None:
        return req.solver_timeout_ms
    return int(os.environ.get("SOLVER_TIMEOUT_MS", "10000"))


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "sql-equivalence-checker",
        "docs": "/docs",
        "check_endpoint": "POST /check",
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
