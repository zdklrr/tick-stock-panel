"""告警记录查询入参边界 — days/limit 越界应被拦下, 合法值照常返回。

同类列表端点 (abnormal.py 的 limit、rps.py 的 days) 已用 Query(ge=..., le=...)
约束同名参数; 本文件锁住 /api/alerts 的同一口径。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.alerts import router
from app.services import alert_store


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _seed(tmp_path, count: int = 3) -> None:
    now_ms = int(time.time() * 1000)
    alert_store.append_many(
        tmp_path,
        [
            {"ts": now_ms - i * 1000, "rule_id": f"r{i}", "source": "monitor", "type": "price"}
            for i in range(count)
        ],
    )


@pytest.mark.parametrize("params", [{"days": -1}, {"days": 0}, {"limit": -1}, {"limit": 0}])
def test_out_of_range_params_are_rejected(tmp_path, params):
    _seed(tmp_path)
    resp = _client(tmp_path).get("/api/alerts", params=params)
    assert resp.status_code == 422, resp.text


def test_days_and_limit_above_store_retention_are_rejected(tmp_path):
    _seed(tmp_path)
    client = _client(tmp_path)
    # 存储侧只保留 MAX_DAYS 天 / MAX_RECORDS 条, 超出上限的请求没有可返回的数据
    assert client.get("/api/alerts", params={"days": alert_store.MAX_DAYS + 1}).status_code == 422
    assert client.get(
        "/api/alerts", params={"limit": alert_store.MAX_RECORDS + 1}
    ).status_code == 422


def test_valid_params_still_return_all_records(tmp_path):
    _seed(tmp_path, 3)
    client = _client(tmp_path)
    # 默认值
    body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 3
    assert body["total"] == 3
    # 显式边界值 (前端实际用的 days=7 / limit=1、10、500 均在内)
    for params in (
        {"days": 1, "limit": 1},
        {"days": 7, "limit": 500},
        {"days": alert_store.MAX_DAYS, "limit": alert_store.MAX_RECORDS},
    ):
        resp = client.get("/api/alerts", params=params)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["alerts"]) == min(3, params["limit"])


def _client_with_names(tmp_path, name_map: dict[str, str]) -> TestClient:
    """带 get_name_map 的 repo 桩 — 复现真实 repo 的名称解析入口。"""
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_name_map=lambda symbols=None: {
            s: n for s, n in name_map.items() if symbols is None or s in set(symbols)
        },
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app)


def test_missing_name_is_backfilled_on_read(tmp_path):
    """v0.3.30 之前落盘的模拟盘事件没有 name, 读时应按 symbol 回填。"""
    now_ms = int(time.time() * 1000)
    alert_store.append_many(
        tmp_path,
        [
            {
                "ts": now_ms,
                "source": "paper",
                "type": "auto_order",
                "symbol": "600327.SH",
                "message": "自动跟单触发买入下单 (次日开盘) 700股",
            },
            {
                "ts": now_ms - 1000,
                "source": "paper",
                "type": "auto_order",
                "symbol": "000001.SZ",
                "name": "平安银行",  # 已有名称不能被覆盖
                "message": "自动跟单买入 平安银行(000001.SZ) 100股",
            },
        ],
    )
    client = _client_with_names(
        tmp_path, {"600327.SH": "大东方", "000001.SZ": "改名了"}
    )
    alerts = client.get("/api/alerts").json()["alerts"]
    by_symbol = {a["symbol"]: a for a in alerts}
    assert by_symbol["600327.SH"]["name"] == "大东方"
    assert by_symbol["000001.SZ"]["name"] == "平安银行"


def test_name_backfill_survives_repo_without_name_map(tmp_path):
    """repo 没有 get_name_map 时不能 500, 记录原样返回。"""
    _seed(tmp_path, 1)
    resp = _client(tmp_path).get("/api/alerts")
    assert resp.status_code == 200
    assert len(resp.json()["alerts"]) == 1
