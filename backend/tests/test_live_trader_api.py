"""实盘扩展 HTTP 层测试: 只验证路由契约与错误映射, 不触碰 GUI。

lifespan 会真的建 LiveService(默认 LiveBroker), 但不会连接客户端 ——
所有端点里只有 /connect、/account、/entrusts、/trades、/cancel-all 会碰 GUI,
本文件不调用它们。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.live_trader.routes import build_router


@pytest.fixture
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    app.include_router(build_router())
    with TestClient(app) as c:
        yield c


def test_status_returns_defaults(client):
    body = client.get("/api/custom/live-trader/status").json()
    assert body["config"]["enabled"] is False
    assert body["config"]["dry_run"] is True
    assert body["broker"]["connected"] is False
    assert body["limits"]["max_orders_per_day"] == 10
    assert body["limits"]["remaining_orders"] == 10


def test_schema_exposes_defaults(client):
    body = client.get("/api/custom/live-trader/schema").json()
    assert body["defaults"]["dry_run"] is True
    assert body["defaults"]["max_order_amount"] == 20000.0


def test_config_partial_update_and_persist(client):
    r = client.post("/api/custom/live-trader/config", json={"max_orders_per_day": 3})
    assert r.status_code == 200
    assert r.json()["config"]["max_orders_per_day"] == 3
    # 未传的字段保持不变
    assert r.json()["config"]["dry_run"] is True
    again = client.get("/api/custom/live-trader/status").json()
    assert again["config"]["max_orders_per_day"] == 3
    assert again["limits"]["remaining_orders"] == 3


def test_config_rejects_unknown_field(client):
    r = client.post("/api/custom/live-trader/config", json={"nope": 1})
    assert r.status_code == 422


def test_config_rejects_out_of_range(client):
    r = client.post("/api/custom/live-trader/config", json={"max_orders_per_day": 999})
    assert r.status_code == 422


def test_orders_endpoint_empty(client):
    body = client.get("/api/custom/live-trader/orders").json()
    assert body["orders"] == []


def test_mirror_without_accounts_returns_empty(client):
    body = client.post("/api/custom/live-trader/mirror").json()
    assert body["results"] == []


def test_mirror_disabled_returns_empty(client):
    client.post("/api/custom/live-trader/config", json={"enabled": True, "accounts": ["default"]})
    # 无账户数据 → 扫不到单, 不会抛
    body = client.post("/api/custom/live-trader/mirror").json()
    assert body["results"] == []


def test_account_without_connection_returns_409(client):
    r = client.get("/api/custom/live-trader/account")
    assert r.status_code == 409
    assert "尚未连接" in r.json()["detail"]


def test_disconnect_without_connection_is_ok(client):
    r = client.post("/api/custom/live-trader/disconnect")
    assert r.status_code == 200
    assert r.json()["broker"]["connected"] is False
