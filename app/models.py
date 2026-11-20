"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import INT_MAX, INT_MIN, MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )


# ---------- 待补测读数影响分析（/alert-bounds） ----------


class StationSnapshotRow(BaseModel):
    id: int = Field(..., ge=INT_MIN, le=INT_MAX, description="站点 id（主键，非空）")
    threshold: int | None = Field(
        None, ge=INT_MIN, le=INT_MAX, description="告警阈值列，可 NULL"
    )


class ReadingSnapshotRow(BaseModel):
    station_id: int | None = Field(
        None, ge=INT_MIN, le=INT_MAX, description="外键，NULL 合法"
    )
    value: int | None = Field(
        None, ge=INT_MIN, le=INT_MAX, description="NULL 表示待补测"
    )


class FillRange(BaseModel):
    row: int = Field(..., ge=0, description="readings 输入行号（0 起始）")
    min: int = Field(..., ge=INT_MIN, le=INT_MAX, description="补测取值下界（含）")
    max: int = Field(..., ge=INT_MIN, le=INT_MAX, description="补测取值上界（含）")


class AlertBoundsRequest(BaseModel):
    stations: list[StationSnapshotRow] = Field(
        ..., max_length=MAX_ROWS, description="stations 快照（至多 3 行）"
    )
    readings: list[ReadingSnapshotRow] = Field(
        ..., max_length=MAX_ROWS, description="readings 快照（至多 3 行）"
    )
    fill_ranges: list[FillRange] = Field(
        default_factory=list,
        description="按 readings 输入行号为每个待补测 NULL value 指定整数闭区间",
    )
    alert_threshold: int = Field(
        ..., ge=1, description="告警条数阈值（达到即需处置）"
    )
    query: str = Field(
        ...,
        description="质控 SELECT：以 stations 为 FROM 表，仅投影 stations.id；"
        "连接与筛选沿用受支持语法子集",
    )
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="单次 z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
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
                }
            ]
        }
    }


class FillAssignment(BaseModel):
    row: int = Field(..., description="readings 输入行号（0 起始）")
    value: int = Field(..., description="该行的补测取值")


class ExtremumInfo(BaseModel):
    count: int = Field(..., description="极值行数（已在隔离 PostgreSQL 复算确认）")
    fills: list[FillAssignment] = Field(
        ..., description="达到该极值的一组完整补测值"
    )


class StationAlertBounds(BaseModel):
    station_id: int = Field(..., description="站点 id")
    min_count: int = Field(..., description="所有合法补测取值下的最少结果行数")
    max_count: int = Field(..., description="所有合法补测取值下的最多结果行数")
    pending: bool = Field(
        ...,
        description="待补测标记：最少行数未达告警阈值、最多行数达到告警阈值",
    )
    min_extremum: ExtremumInfo = Field(..., description="最少行数及其补测见证")
    max_extremum: ExtremumInfo = Field(..., description="最多行数及其补测见证")


class AlertBoundsResponse(BaseModel):
    status: Literal["OK", "UNKNOWN"] = Field(
        ...,
        description="OK=每站上下界均已求解并在隔离 PostgreSQL 复算确认；"
        "UNKNOWN=求解或复算无法确认",
    )
    alert_threshold: int = Field(..., description="本次判定使用的告警条数阈值")
    stations: list[StationAlertBounds] | None = Field(
        None, description="status=OK 时每站的行数上下界与待补测标记"
    )
    message: str = Field(..., description="判定说明")
