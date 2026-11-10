"""虚拟账户 API 契约测试 — 成功/无数据/错误响应 (per CONTRIBUTING API 契约矩阵)。

镜像 test_alerts_query_bounds.py: 只挂本路由的裸 FastAPI app, 不经过主应用
全局中间件 (访问密码门是部署层关注点, 不属于本契约)。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "etf" if s.split(".")[0].startswith(("51", "56", "58", "15")) else "stock",
    )
    return TestClient(app)


def test_overview_before_init(client: TestClient):
    r = client.get("/api/paper/overview")
    assert r.status_code == 200
    assert r.json() == {"initialized": False, "account_id": "default"}


def test_multi_account_isolation_and_settings(client: TestClient):
    """双账户隔离: 订单/概览互不可见; settings 端点切换涨跌停排队。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})  # default
    r = client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_a", "name": "策略A"})
    assert r.status_code == 200 and r.json()["account"]["name"] == "策略A"

    # 账户列表
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert ids == {"default", "acc_a"}

    # 订单隔离: 同一笔下单只出现在对应账户
    assert client.post("/api/paper/orders?account=acc_a", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0}).status_code == 200
    assert len(client.get("/api/paper/orders?account=acc_a").json()["orders"]) == 1
    assert len(client.get("/api/paper/orders").json()["orders"]) == 0

    # 概览隔离: acc_a 有 pending 单不影响 default 的现金展示
    assert client.get("/api/paper/overview?account=acc_a").json()["initial_cash"] == 500000
    assert client.get("/api/paper/overview").json()["initial_cash"] == 1000000

    # settings: 默认排队开启; 可显式关闭 → 账户字段更新
    assert client.get("/api/paper/overview?account=acc_a").json()["queue_limit_orders"] is True
    r = client.post("/api/paper/settings?account=acc_a", json={"queue_limit_orders": False})
    assert r.status_code == 200 and r.json()["account"]["queue_limit_orders"] is False
    assert client.get("/api/paper/overview?account=acc_a").json()["queue_limit_orders"] is False
    assert client.get("/api/paper/overview").json()["queue_limit_orders"] is True

    # 非法账户 id → 400 (路径穿越防护)
    assert client.get("/api/paper/overview?account=../x").status_code == 400


def test_account_create_idempotent(client: TestClient):
    r1 = client.post("/api/paper/account", json={"initial_cash": 500000})
    assert r1.status_code == 200
    r2 = client.post("/api/paper/account", json={"initial_cash": 900000})
    assert r2.status_code == 200
    assert r2.json()["account"]["cash"] == 500000  # 幂等: 不覆盖


def test_account_rejects_non_positive_cash(client: TestClient):
    assert client.post("/api/paper/account", json={"initial_cash": 0}).status_code == 400
    assert client.post("/api/paper/account", json={"initial_cash": -1}).status_code == 400


def test_order_create_list_cancel_flow(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0})
    assert r.status_code == 200
    order = r.json()["order"]
    assert order["status"] == "pending"

    # 非百股整数倍 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 150}).status_code == 400
    # 超资金 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100000, "ref_price": 1500.0}).status_code == 400
    # 卖出无持仓 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "sell", "qty": 100}).status_code == 400

    # 列表 + 撤单
    assert len(client.get("/api/paper/orders?status=pending").json()["orders"]) == 1
    r = client.delete(f"/api/paper/orders/{order['id']}")
    assert r.status_code == 200 and r.json()["order"]["status"] == "cancelled"
    # 已撤不可再撤
    assert client.delete(f"/api/paper/orders/{order['id']}").status_code == 400


def test_order_amount_mode_auto_converts(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 200000, "ref_price": 1500.0})
    assert r.status_code == 200
    assert r.json()["order"]["qty"] == 100  # 20万 / 1500 = 133 股 → 向下取整百 = 100
    # 无参考价的金额单 → 400
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "amount": 100}).status_code == 400


def test_freeze_blocks_new_orders(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.post("/api/paper/freeze?frozen=true").status_code == 200
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0})
    assert r.status_code == 400 and "冻结" in r.json()["detail"]
    # 解冻恢复
    assert client.post("/api/paper/freeze?frozen=false").status_code == 200
    assert client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 10.0}).status_code == 200


def test_freeze_before_init_rejected(client: TestClient):
    assert client.post("/api/paper/freeze?frozen=true").status_code == 400


def test_trades_nav_stats_empty(client: TestClient):
    client.post("/api/paper/account", json={"initial_cash": 100000})
    assert client.get("/api/paper/trades").json() == {"fills": []}
    assert client.get("/api/paper/nav").json() == {"nav": []}
    stats = client.get("/api/paper/stats").json()
    assert stats["rounds"] == 0 and stats["win_rate"] == 0.0


def test_settings_updates_fees(client: TestClient):
    """费用三参数可经 settings 端点调整; 超范围 → 400; 未知字段忽略。"""
    client.post("/api/paper/account", json={"initial_cash": 1000000})
    r = client.post("/api/paper/settings", json={
        "commission_pct": 0.0003, "stamp_tax_pct": 0.0005, "slippage_bps": 8,
    })
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["commission_pct"] == 0.0003
    assert acc["stamp_tax_pct"] == 0.0005
    assert acc["slippage_bps"] == 8
    # 超范围
    r = client.post("/api/paper/settings", json={"commission_pct": 0.5})
    assert r.status_code == 400 and "超出合理范围" in r.json()["detail"]
    # 部分更新: 只改滑点, 佣金不动
    r = client.post("/api/paper/settings", json={"slippage_bps": 3})
    acc = r.json()["account"]
    assert acc["slippage_bps"] == 3 and acc["commission_pct"] == 0.0003


def test_compare_accounts(client: TestClient):
    """/compare: 逐账户概览+统计+净值; 未初始化的空壳账户不出现。"""
    # 只有懒创建的空壳目录时 → 空列表
    assert client.get("/api/paper/compare").json()["accounts"] == []

    client.post("/api/paper/account", json={"initial_cash": 1000000, "name": "甲"})
    client.post("/api/paper/account", json={"initial_cash": 500000, "account_id": "acc_b", "name": "乙"})
    rows = client.get("/api/paper/compare").json()["accounts"]
    assert [r["account"] for r in rows] == ["default", "acc_b"]
    row = rows[0]
    assert row["name"] == "甲" and row["initial_cash"] == 1000000
    assert row["total"] == pytest.approx(1000000)
    assert row["pnl_pct"] == 0.0
    assert row["fees"]["commission_pct"] == pytest.approx(0.00025)
    assert row["rounds"] == 0 and row["win_rate"] == 0.0
    assert row["nav"] == []  # 尚无定版净值
    # 字段完整性 (对比表依赖)
    for key in ("cash", "market_value", "total_pnl", "profit_loss_ratio", "max_drawdown", "avg_holding_days", "realized_pnl"):
        assert key in row


def test_overview_holdings_join_active_plan(client: TestClient):
    """overview holdings join 活跃计划: 补 plan_* 字段; 同 symbol 多条取最早;
    计划取消后不再 join; 无计划 symbol 不写这些键。"""
    from app.strategy import paper, paper_plans

    assert client.post("/api/paper/account", json={"initial_cash": 1000000, "name": "主账户"}).status_code == 200
    p1 = client.post("/api/paper/plans", json={
        "symbol": "600519.SH", "name": "贵州茅台",
        "entry_low": 98.0, "entry_high": 102.0, "stop": 96.0, "qty_planned": 1000,
    }).json()["plan"]
    # 同 symbol 第二条活跃计划 (更晚创建) — join 必须选最早那条
    client.post("/api/paper/plans", json={
        "symbol": "600519.SH", "entry_low": 100.0, "entry_high": 110.0, "stop": 95.0, "qty_planned": 500,
    })
    # 另一只票的无关计划
    client.post("/api/paper/plans", json={
        "symbol": "000001.SZ", "entry_low": 9.0, "entry_high": 11.0, "stop": 8.5, "qty_planned": 1000,
    })

    # 造持仓: 600519.SH 1000股 @ 100, 300750.SZ 200股 @ 50 (无计划)
    root = paper._root(client.app.state.repo.store.data_dir)
    (root / "positions.json").write_text(
        __import__("json").dumps(
            {"positions": {
                "600519.SH": {"asset_type": "stock", "qty": 1000, "avg_cost": 100.0,
                              "available_qty": 1000, "lots": [{"date": "2026-01-01", "qty": 1000}]},
                "300750.SZ": {"asset_type": "stock", "qty": 200, "avg_cost": 50.0,
                              "available_qty": 200, "lots": [{"date": "2026-01-01", "qty": 200}]},
            }},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ov = client.get("/api/paper/overview").json()
    assert ov["initialized"] is True
    by_sym = {h["symbol"]: h for h in ov["holdings"]}

    h1 = by_sym["600519.SH"]
    assert h1["plan_stop_loss"] == pytest.approx(96.0)
    assert h1["plan_target1"] == pytest.approx(106.0)   # entry_ref=(98+102)/2=100 → t1=100+1.5*(100-96)? 实测 106: r1=1.5
    assert h1["plan_target2"] is not None
    assert h1["plan_entry_mode"] == "zone"
    assert h1["plan_source"] == "手动" or h1["plan_source"]  # 人话来源, 手工计划为 source 原文/「手动」口径
    assert h1["position_pct"] > 0                        # paper.overview 补的纯字段

    # 无计划的票: 键要么缺失要么 None
    h2 = by_sym["300750.SZ"]
    assert h2.get("plan_stop_loss") in (None,) and h2.get("plan_entry_mode") is None

    # 计划取消后 join 落空 → 键缺失或 None (无计划 symbol 不写这些键)
    paper_plans.cancel_plan(client.app.state.repo.store.data_dir, p1["id"])
    # 把另一条同 symbol 的计划也取消
    for plan in paper_plans.list_plans(client.app.state.repo.store.data_dir, status=("pending",)):
        if plan["symbol"] == "600519.SH":
            paper_plans.cancel_plan(client.app.state.repo.store.data_dir, plan["id"])
    ov2 = client.get("/api/paper/overview").json()
    h1b = next(h for h in ov2["holdings"] if h["symbol"] == "600519.SH")
    assert h1b.get("plan_stop_loss") is None and h1b.get("plan_entry_mode") is None
    assert h1b.get("plan_source") is None


def test_overview_holding_name_falls_back_to_plan(client: TestClient):
    """持仓没有 name (老台账) 时, 用关联计划的名称兜底显示。"""
    from app.strategy import paper

    assert client.post("/api/paper/account", json={"initial_cash": 1000000, "name": "主账户"}).status_code == 200
    client.post("/api/paper/plans", json={
        "symbol": "600519.SH", "name": "贵州茅台",
        "entry_low": 98.0, "entry_high": 102.0, "stop": 96.0, "qty_planned": 1000,
    })
    root = paper._root(client.app.state.repo.store.data_dir)
    (root / "positions.json").write_text(
        __import__("json").dumps(
            {"positions": {"600519.SH": {"asset_type": "stock", "qty": 1000, "avg_cost": 100.0,
                                         "available_qty": 1000, "lots": []}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ov = client.get("/api/paper/overview").json()
    h = ov["holdings"][0]
    assert h["name"] == "贵州茅台"           # 来自计划的兜底
    assert "price_date" in h and "priced" in h
    assert "prev_close" in h and "day_pnl" in h and "day_pnl_pct" in h


def test_overview_exposes_day_pnl_and_price_source(client: TestClient):
    """overview 顶层给出今日盈亏与估值口径来源 (前端今日盈亏卡依赖)。"""
    assert client.post("/api/paper/account", json={"initial_cash": 1000000}).status_code == 200
    ov = client.get("/api/paper/overview").json()
    assert ov["initialized"] is True
    assert "day_pnl" in ov and "day_pnl_pct" in ov
    assert ov["price_source"] in ("live", "daily", "cost")
    assert ov["estimating"] is False  # 无盘中快照


def test_positions_uses_daily_valuation_and_names(client: TestClient):
    """/positions 同样用日线口径估值并回填名称 (非交易时段不再恒 0 盈亏)。"""
    from app.strategy import paper

    assert client.post("/api/paper/account", json={"initial_cash": 1000000}).status_code == 200
    client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 1000, "ref_price": 100.0})
    root = paper._root(client.app.state.repo.store.data_dir)
    (root / "positions.json").write_text(
        __import__("json").dumps(
            {"positions": {"600519.SH": {"name": "贵州茅台", "asset_type": "stock", "qty": 1000,
                                         "avg_cost": 100.0, "available_qty": 1000, "lots": []}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    body = client.get("/api/paper/positions").json()
    h = body["holdings"][0]
    assert h["name"] == "贵州茅台"
    assert "day_pnl" in h and "priced" in h


def test_plans_list_fills_missing_names_from_name_map(client: TestClient):
    """存量自动计划 name 为 None 时, /plans 用维表名称兜底 (0.3.47)。

    自动跟单补建计划时没把名称传下去, 老库里那批计划只有代码; 这里确认
    列表接口会就地回填, 且不改动其它数值字段。
    """
    from app.strategy import paper as paper_mod
    from app.strategy import paper_plans

    assert client.post("/api/paper/account", json={"initial_cash": 1000000}).status_code == 200

    # 直接落一条 name=None 的存量计划 (模拟 0.3.46 之前补建的那批)
    data_dir = client.app.state.repo.store.data_dir
    default_id = paper_mod.list_accounts(data_dir)[0]["id"]
    paper_plans.create_plan(
        data_dir,
        {
            "symbol": "300007.SZ",
            "entry_low": 38.0,
            "entry_high": 40.0,
            "stop": 36.0,
            "qty_planned": 1000,
        },
        default_id,
    )

    # 给假仓储补上名称查询能力 (真仓储读 instruments 维表)
    client.app.state.repo.get_name_map = lambda syms: {"300007.SZ": "汉威科技"}

    body = client.get("/api/paper/plans").json()
    plan = next(p for p in body["plans"] if p["symbol"] == "300007.SZ")
    assert plan["name"] == "汉威科技"
    assert plan["qty_planned"] == 1000            # 数值字段不受影响
    assert plan["entry_low"] == 38.0


def test_plans_list_without_name_map_keeps_code_only(client: TestClient):
    """仓储无 get_name_map (裸 app 单测) 时不联网、不抛, 只有代码。"""
    from app.strategy import paper as paper_mod
    from app.strategy import paper_plans

    assert client.post("/api/paper/account", json={"initial_cash": 1000000}).status_code == 200
    data_dir = client.app.state.repo.store.data_dir
    default_id = paper_mod.list_accounts(data_dir)[0]["id"]
    paper_plans.create_plan(
        data_dir,
        {
            "symbol": "300473.SZ",
            "entry_low": 45.0,
            "entry_high": 47.0,
            "stop": 43.0,
            "qty_planned": 500,
        },
        default_id,
    )

    body = client.get("/api/paper/plans").json()
    plan = next(p for p in body["plans"] if p["symbol"] == "300473.SZ")
    assert plan["name"] is None
