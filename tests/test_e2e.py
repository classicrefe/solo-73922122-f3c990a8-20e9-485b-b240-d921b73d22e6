"""端到端测试：需要真实 PostgreSQL（环境变量 DATABASE_URL）。

本地无 Docker 时可用 pgserver 提供临时实例：
    DATABASE_URL=$(python -c ...) pytest tests/test_e2e.py
在 docker compose 环境中，DATABASE_URL 指向 db 服务。
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 未设置，跳过端到端测试"
)

client = TestClient(app)


def test_e2e_not_equivalent_reproduced_in_postgres():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold = 5",
            "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_EQUIVALENT"
    cx = body["counterexample"]
    assert "CREATE TABLE stations" in cx["setup_sql"]
    # 两侧结果确实不同，且 b 侧多出 NULL threshold 的站点
    assert sorted(map(tuple, cx["query_a_result"])) != sorted(
        map(tuple, cx["query_b_result"])
    )


def test_e2e_equivalent():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id WHERE r.value IS NULL",
            "query_b": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id WHERE r.value IS NULL "
                       "AND (s.threshold > -2147483648 OR s.threshold <= -2147483648 "
                       "OR s.threshold IS NULL)",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "EQUIVALENT"


def test_e2e_left_join_duplicate_witness():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id",
            "query_b": "SELECT id FROM stations",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_EQUIVALENT"
    # 反例装载 SQL 可直接执行（在复现事务里已跑通过）
    assert "INSERT INTO readings" in body["counterexample"]["setup_sql"]


def test_e2e_health_db_up():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["database"] == "up"


# ---------- /alert-bounds 端到端 ----------


def test_e2e_alert_bounds_pending_station():
    resp = client.post(
        "/alert-bounds",
        json={
            "stations": [{"id": 1, "threshold": 5}, {"id": 2, "threshold": 5}],
            "readings": [
                {"station_id": 1, "value": 7},
                {"station_id": 1, "value": None},
                {"station_id": 2, "value": 3},
            ],
            "fill_ranges": [{"row": 1, "min": 0, "max": 10}],
            "alert_threshold": 2,
            "query": (
                "SELECT s.id FROM stations s INNER JOIN readings r "
                "ON s.id = r.station_id WHERE r.value >= 5"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    stations = {s["station_id"]: s for s in body["stations"]}
    assert (stations[1]["min_count"], stations[1]["max_count"]) == (1, 2)
    assert stations[1]["pending"] is True
    assert (stations[2]["min_count"], stations[2]["max_count"]) == (0, 0)
    assert stations[2]["pending"] is False
    # 极值补测见证已在隔离 PostgreSQL 中复算确认
    assert stations[1]["min_extremum"]["fills"][0]["row"] == 1
    assert 0 <= stations[1]["min_extremum"]["fills"][0]["value"] <= 4
    assert 5 <= stations[1]["max_extremum"]["fills"][0]["value"] <= 10


def test_e2e_alert_bounds_left_join():
    resp = client.post(
        "/alert-bounds",
        json={
            "stations": [{"id": 1, "threshold": None}],
            "readings": [{"station_id": 1, "value": None}],
            "fill_ranges": [{"row": 0, "min": 0, "max": 10}],
            "alert_threshold": 1,
            "query": (
                "SELECT s.id FROM stations s LEFT JOIN readings r "
                "ON s.id = r.station_id WHERE r.value >= 5 OR r.value IS NULL"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    (s1,) = body["stations"]
    assert (s1["min_count"], s1["max_count"]) == (0, 1)
    assert s1["pending"] is True


def test_e2e_alert_bounds_no_null_values():
    resp = client.post(
        "/alert-bounds",
        json={
            "stations": [{"id": 1, "threshold": 9}],
            "readings": [
                {"station_id": 1, "value": 1},
                {"station_id": 1, "value": 2},
            ],
            "fill_ranges": [],
            "alert_threshold": 2,
            "query": (
                "SELECT s.id FROM stations s INNER JOIN readings r "
                "ON s.id = r.station_id WHERE r.value > 0"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    (s1,) = body["stations"]
    assert (s1["min_count"], s1["max_count"]) == (2, 2)
    assert s1["pending"] is False
    assert s1["min_extremum"]["fills"] == []
