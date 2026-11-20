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
