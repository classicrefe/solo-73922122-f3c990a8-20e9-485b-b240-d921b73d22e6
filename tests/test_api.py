"""API 行为测试：解析错误、列数不一致、EQUIVALENT、UNKNOWN（库不可用）、
NOT_EQUIVALENT（打桩复现器）。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_equivalent_no_db_needed():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations WHERE threshold >= 1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "EQUIVALENT"
    assert body["bound_rows"] == 3
    assert body["counterexample"] is None


def test_rejected_sql_returns_400():
    resp = client.post(
        "/check",
        json={"query_a": "SELECT * FROM stations", "query_b": "SELECT id FROM stations"},
    )
    assert resp.status_code == 400
    assert "受支持子集" in resp.json()["detail"]


def test_arity_mismatch_returns_400():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations",
            "query_b": "SELECT id, threshold FROM stations",
        },
    )
    assert resp.status_code == 400


def test_unknown_when_db_unavailable(monkeypatch):
    """z3 找到候选反例但数据库不可达 -> UNKNOWN。"""
    from app import main

    class DownVerifier:
        def __init__(self, dsn):
            pass

        def reproduce(self, witness, qa, qb):
            from app.verifier import ReproOutcome

            return ReproOutcome(ok=False, detail="PostgreSQL 复现失败: 连接被拒")

    monkeypatch.setattr(main, "Verifier", DownVerifier)
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "UNKNOWN"


def test_not_equivalent_with_stubbed_repro(monkeypatch):
    """复现确认差异 -> NOT_EQUIVALENT，且返回可装载 SQL 与两侧结果。"""
    from app import main
    from app.verifier import ReproOutcome

    class DiffVerifier:
        def __init__(self, dsn):
            pass

        def reproduce(self, witness, qa, qb):
            return ReproOutcome(
                ok=True,
                differs=True,
                query_a_result=[],
                query_b_result=[[1]],
            )

    monkeypatch.setattr(main, "Verifier", DiffVerifier)
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_EQUIVALENT"
    cx = body["counterexample"]
    assert "CREATE TABLE stations" in cx["setup_sql"]
    assert "INSERT INTO stations" in cx["setup_sql"]
    assert cx["query_a_result"] == []
    assert cx["query_b_result"] == [[1]]


def test_unknown_when_repro_not_confirmed(monkeypatch):
    """z3 反例在 PostgreSQL 中未复现差异 -> UNKNOWN（防御编码错误）。"""
    from app import main
    from app.verifier import ReproOutcome

    class SameVerifier:
        def __init__(self, dsn):
            pass

        def reproduce(self, witness, qa, qb):
            return ReproOutcome(
                ok=True, differs=False, query_a_result=[[1]], query_b_result=[[1]]
            )

    monkeypatch.setattr(main, "Verifier", SameVerifier)
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "UNKNOWN"


def test_health_endpoint_shape():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["database"] in ("up", "down")


def test_openapi_docs_available():
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/check" in spec["paths"]
