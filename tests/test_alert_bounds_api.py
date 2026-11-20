"""/alert-bounds API 行为测试：快照校验、查询形态校验、OK / UNKNOWN 路径。

复算器用打桩实现：MiniVerifier 以独立的 Python 求值器模拟 INNER JOIN +
value 过滤的查询语义，与 z3 极值交叉验证。
"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.verifier import QueryRowsOutcome

client = TestClient(app)

# 测试查询：INNER JOIN + r.value >= 5
JOIN_QUERY = (
    "SELECT s.id FROM stations s INNER JOIN readings r "
    "ON s.id = r.station_id WHERE r.value >= 5"
)


class MiniVerifier:
    """打桩复算器：用 Python 实际求值 JOIN_QUERY 语义。"""

    def __init__(self, dsn):
        pass

    def run_query(self, stations, readings, query):
        assert query == JOIN_QUERY
        rows = []
        for sid, _th in stations:
            for rsid, val in readings:
                if rsid == sid and val is not None and val >= 5:
                    rows.append([sid])
        return QueryRowsOutcome(ok=True, rows=rows)


@pytest.fixture
def mini_verifier(monkeypatch):
    monkeypatch.setattr(main, "Verifier", MiniVerifier)


def _payload(**overrides):
    payload = {
        "stations": [{"id": 1, "threshold": 5}, {"id": 2, "threshold": 5}],
        "readings": [
            {"station_id": 1, "value": 7},
            {"station_id": 1, "value": None},
            {"station_id": 2, "value": 3},
        ],
        "fill_ranges": [{"row": 1, "min": 0, "max": 10}],
        "alert_threshold": 2,
        "query": JOIN_QUERY,
    }
    payload.update(overrides)
    return payload


# ---------- OK 路径 ----------


def test_ok_pending_station(mini_verifier):
    resp = client.post("/alert-bounds", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["alert_threshold"] == 2
    stations = {s["station_id"]: s for s in body["stations"]}
    s1, s2 = stations[1], stations[2]
    # 站 1：min=1（fill<5）未达阈值 2，max=2（fill>=5）达阈值 -> 待补测
    assert (s1["min_count"], s1["max_count"]) == (1, 2)
    assert s1["pending"] is True
    # 站 2：恒 0 行，不待补测
    assert (s2["min_count"], s2["max_count"]) == (0, 0)
    assert s2["pending"] is False
    # 每个极值附完整补测值（覆盖待补测行 1）
    for ext in (s1["min_extremum"], s1["max_extremum"]):
        assert [f["row"] for f in ext["fills"]] == [1]
    min_fill = s1["min_extremum"]["fills"][0]["value"]
    max_fill = s1["max_extremum"]["fills"][0]["value"]
    assert 0 <= min_fill <= 4
    assert 5 <= max_fill <= 10
    assert s1["min_extremum"]["count"] == 1
    assert s1["max_extremum"]["count"] == 2


def test_ok_no_null_values(mini_verifier):
    resp = client.post(
        "/alert-bounds",
        json=_payload(
            readings=[{"station_id": 1, "value": 9}],
            fill_ranges=[],
            alert_threshold=1,
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    stations = {s["station_id"]: s for s in body["stations"]}
    assert (stations[1]["min_count"], stations[1]["max_count"]) == (1, 1)
    assert stations[1]["pending"] is False
    assert stations[1]["min_extremum"]["fills"] == []


def test_ok_threshold_boundary(mini_verifier):
    # max 恰好等于阈值即算"达到阈值"
    resp = client.post("/alert-bounds", json=_payload(alert_threshold=2))
    body = resp.json()
    assert body["stations"][0]["pending"] is True
    # 阈值 3：max=2 未达 -> 不待补测
    resp = client.post("/alert-bounds", json=_payload(alert_threshold=3))
    assert resp.json()["stations"][0]["pending"] is False
    # 阈值 1：min=1 已达 -> 不属"最少时未达"
    resp = client.post("/alert-bounds", json=_payload(alert_threshold=1))
    assert resp.json()["stations"][0]["pending"] is False


# ---------- UNKNOWN 路径 ----------


def test_unknown_when_db_unavailable(monkeypatch):
    class DownVerifier:
        def __init__(self, dsn):
            pass

        def run_query(self, stations, readings, query):
            return QueryRowsOutcome(ok=False, detail="PostgreSQL 复算失败: 连接被拒")

    monkeypatch.setattr(main, "Verifier", DownVerifier)
    resp = client.post("/alert-bounds", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "UNKNOWN"
    assert body["stations"] is None
    assert "复算失败" in body["message"]


def test_unknown_when_recount_mismatches(monkeypatch):
    """复算行数与 z3 极值不符 -> UNKNOWN（防御编码错误）。"""

    class EmptyVerifier:
        def __init__(self, dsn):
            pass

        def run_query(self, stations, readings, query):
            return QueryRowsOutcome(ok=True, rows=[])

    monkeypatch.setattr(main, "Verifier", EmptyVerifier)
    resp = client.post("/alert-bounds", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "UNKNOWN"
    assert "内部不一致" in body["message"]


# ---------- 400 校验 ----------


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"stations": [{"id": 1, "threshold": 1}, {"id": 1, "threshold": 2}]},
         "主键重复"),
        ({"readings": [{"station_id": 99, "value": 1}]}, "外键悬空"),
        ({"fill_ranges": [{"row": 5, "min": 0, "max": 1}]}, "行号越界"),
        ({"fill_ranges": [{"row": 0, "min": 0, "max": 1},
                          {"row": 1, "min": 0, "max": 1}]}, "非 NULL"),
        ({"fill_ranges": [{"row": 1, "min": 0, "max": 1},
                          {"row": 1, "min": 2, "max": 3}]}, "行号重复"),
        ({"fill_ranges": [{"row": 1, "min": 6, "max": 5}]}, "下界大于上界"),
        ({"fill_ranges": []}, "待补测"),
        ({"query": "SELECT station_id FROM readings"}, "FROM 表"),
        ({"query": "SELECT id, threshold FROM stations"}, "只能投影"),
        ({"query": "SELECT r.station_id FROM stations s "
                   "JOIN readings r ON s.id = r.station_id"}, "只能投影"),
        ({"query": "SELECT * FROM stations"}, "受支持子集"),
        ({"query": "SELECT id FROM stations WHERE id IN (1,2)"}, "受支持子集"),
    ],
)
def test_validation_400(overrides, fragment):
    resp = client.post("/alert-bounds", json=_payload(**overrides))
    assert resp.status_code == 400, resp.json()
    assert fragment in resp.json()["detail"]


def test_schema_validation_422():
    # 超过每表 3 行
    resp = client.post(
        "/alert-bounds",
        json=_payload(
            stations=[{"id": i, "threshold": 0} for i in range(4)],
            readings=[],
            fill_ranges=[],
        ),
    )
    assert resp.status_code == 422
    # int4 之外的值
    resp = client.post(
        "/alert-bounds",
        json=_payload(fill_ranges=[{"row": 1, "min": 0, "max": 2**31}]),
    )
    assert resp.status_code == 422
    # 阈值必须为正
    resp = client.post("/alert-bounds", json=_payload(alert_threshold=0))
    assert resp.status_code == 422


def test_openapi_includes_alert_bounds():
    spec = client.get("/openapi.json").json()
    assert "/alert-bounds" in spec["paths"]
    post = spec["paths"]["/alert-bounds"]["post"]
    examples = post["requestBody"]["content"]["application/json"]["examples"]
    assert "pending-when-fill-high" in examples
