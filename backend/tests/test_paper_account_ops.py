"""模拟盘账户操作契约: 批量删除 / 批量启停 / 重置 / 启动停止别名 / 挑行建计划。

覆盖用户反馈的四类诉求:
  - 「创建了多个账户, 怎么没有多选删除按钮」→ batch_delete / batch_status
  - 「模拟盘界面里怎么没启动/停止/重置功能」→ accounts/{id}/start|stop + reset
  - 「策略选股出来的也应该有个交易计划, 人员可以选择买或者不买」→ plans/from_rows

另有 frozen 语义专项: 冻结账户必须被 撮合 (evaluate_intraday) / 结算 (settle_day) /
计划 (evaluate_plans) / 跟单 (create_order 门禁) 全部跳过, 否则「停止」按钮是假的。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router
from app.strategy import paper, paper_plans
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
DAY = date(2026, 11, 2)
CN = timezone(timedelta(hours=8))


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 paper / paper_plans 的时间锚定到 DAY 10:00 (CST), 并建 default 账户。"""
    fixed_now = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=CN)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: fixed_now)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper_plans, "cn_now", lambda: fixed_now)
    paper.create_account(tmp_path, 1_000_000, account_id="default", name="甲")
    return tmp_path


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def _write_daily(data_dir: Path, rows: list[tuple[str, float, float, float]]) -> None:
    """写日线 (day, open, close, low) 到 kline_daily 分区 (撮合/结算读它)。"""
    repo = KlineRepository(DataStore(data_dir))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM] * len(rows),
        "date": [date.fromisoformat(r[0]) for r in rows],
        "open": [r[1] for r in rows],
        "close": [r[2] for r in rows],
        "low": [r[3] for r in rows],
    }))


def _make_plan(data_dir: Path, **overrides) -> dict:
    payload = {
        "symbol": SYM,
        "name": "贵州茅台",
        "entry_low": 98.0,
        "entry_high": 102.0,
        "stop": 96.0,
        "qty_planned": 1000,
    }
    payload.update(overrides)
    return paper_plans.create_plan(data_dir, payload, "default")


# ── 批量删除 ────────────────────────────────────────────
def test_batch_delete_removes_all_and_is_idempotent_for_unknown(client: TestClient, data_dir: Path):
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_a", "name": "A"})
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_b", "name": "B"})

    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["acc_a", "acc_b"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == ["acc_a", "acc_b"] and body["failed"] == []
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert ids == {"default"}


def test_batch_delete_partial_failure_does_not_block_others(client: TestClient, data_dir: Path):
    """有持仓又没 force → 该账户进 failed, 其它账户照删 (单个失败不阻断整批)。"""
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_hold", "name": "有仓"})
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_free", "name": "空仓"})
    _write_daily(data_dir, [("2026-10-29", 100.0, 100.0, 99.0), ("2026-10-30", 100.0, 100.0, 99.0)])
    # 昨日买入 → 今日可卖, 持仓非空
    paper.create_order(data_dir, SYM, "buy", account_id="acc_hold", qty=100, ref_price=100.0)
    paper.settle_day(data_dir, "2026-10-29", account_id="acc_hold")
    assert paper.load_positions(data_dir, "acc_hold")[SYM]["qty"] == 100

    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["acc_hold", "acc_free"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["deleted"] == ["acc_free"]
    assert [f["account"] for f in body["failed"]] == ["acc_hold"]
    assert "force" in body["failed"][0]["error"]
    ids = {a["id"] for a in client.get("/api/paper/accounts").json()["accounts"]}
    assert ids == {"default", "acc_hold"}

    # force=true 再来一次就能删掉
    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["acc_hold"], "force": True})
    assert r.status_code == 200 and r.json()["deleted"] == ["acc_hold"]


def test_batch_delete_empty_list_rejected(client: TestClient):
    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": []})
    assert r.status_code == 400 and "不能为空" in r.json()["detail"]
    # 全是空串/空白也算空
    assert client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["", "  "]}).status_code == 400


def test_batch_delete_dedups_and_caps(client: TestClient):
    """重复 id 只删一次 (不报「账户不存在」); 超过上限直接 400。"""
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_dup", "name": "D"})
    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["acc_dup", "acc_dup"]})
    assert r.status_code == 200
    assert r.json()["deleted"] == ["acc_dup"] and r.json()["failed"] == []

    too_many = [f"acc_{i}" for i in range(101)]
    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": too_many})
    assert r.status_code == 400 and "最多" in r.json()["detail"]


def test_batch_delete_invalid_account_id_reports_failed(client: TestClient):
    r = client.post("/api/paper/accounts/batch_delete", json={"account_ids": ["../evil"]})
    assert r.status_code == 200
    assert r.json()["deleted"] == []
    assert [f["account"] for f in r.json()["failed"]] == ["../evil"]


# ── 批量启停 + start/stop 别名 ──────────────────────────
def test_batch_status_starts_and_stops(client: TestClient, data_dir: Path):
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_a", "name": "A"})
    client.post("/api/paper/account", json={"initial_cash": 500_000, "account_id": "acc_b", "name": "B"})

    r = client.post("/api/paper/accounts/batch_status", json={"account_ids": ["acc_a", "acc_b"], "status": "frozen"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "frozen"
    assert [d["account"] for d in body["updated"]] == ["acc_a", "acc_b"]
    assert paper.get_account(data_dir, "acc_a")["status"] == "frozen"
    assert paper.get_account(data_dir, "acc_b")["status"] == "frozen"
    # default 未选中不受影响
    assert paper.get_account(data_dir, "default")["status"] == "active"

    r = client.post("/api/paper/accounts/batch_status", json={"account_ids": ["acc_a"], "status": "active"})
    assert r.status_code == 200
    assert paper.get_account(data_dir, "acc_a")["status"] == "active"
    assert paper.get_account(data_dir, "acc_b")["status"] == "frozen"


def test_batch_status_rejects_bad_status_and_empty(client: TestClient):
    assert client.post("/api/paper/accounts/batch_status", json={"account_ids": ["default"], "status": "paused"}).status_code == 400
    assert client.post("/api/paper/accounts/batch_status", json={"account_ids": []}).status_code == 400


def test_batch_status_partial_failure(client: TestClient, data_dir: Path):
    """不存在的账户进 failed, 存在的照改。"""
    r = client.post("/api/paper/accounts/batch_status", json={"account_ids": ["default", "no_such"], "status": "frozen"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert [d["account"] for d in body["updated"]] == ["default"]
    assert [f["account"] for f in body["failed"]] == ["no_such"]
    assert paper.get_account(data_dir, "default")["status"] == "frozen"


def test_start_stop_aliases(client: TestClient, data_dir: Path):
    r = client.post("/api/paper/accounts/default/stop")
    assert r.status_code == 200 and r.json()["account"]["status"] == "frozen"
    assert paper.get_account(data_dir, "default")["status"] == "frozen"

    r = client.post("/api/paper/accounts/default/start")
    assert r.status_code == 200 and r.json()["account"]["status"] == "active"
    assert paper.get_account(data_dir, "default")["status"] == "active"

    # 未创建的账户 → 400 (与 /freeze 一致)
    assert client.post("/api/paper/accounts/no_such/stop").status_code == 400
    # 非法 id (含路径穿越字符) → 400
    assert client.post("/api/paper/accounts/%2e%2e/stop").status_code == 400


# ── frozen 语义: 撮合链路必须真的停下 ────────────────────
def test_frozen_account_skips_intraday_matching(client: TestClient, data_dir: Path):
    """冻结后 evaluate_intraday 不撮合已存在的 pending 单 (停止按钮要能停住存量单)。"""
    paper.create_order(data_dir, SYM, "buy", account_id="default", qty=100, ref_price=100.0)
    assert len([o for o in paper.load_orders(data_dir, "default") if o["status"] == "pending"]) == 1

    client.post("/api/paper/accounts/default/stop")
    assert paper.evaluate_intraday(data_dir, {SYM: 100.0}, account_id="default") == []
    assert paper.load_orders(data_dir, "default")[0]["status"] == "pending"  # 单还在, 没成交也没过期

    client.post("/api/paper/accounts/default/start")
    assert len(paper.evaluate_intraday(data_dir, {SYM: 100.0}, account_id="default")) == 1
    assert paper.load_orders(data_dir, "default")[0]["status"] == "filled"


def test_frozen_account_skips_settle_day(client: TestClient, data_dir: Path):
    """冻结后 settle_day 不撮合顺延单、不写净值。"""
    _write_daily(data_dir, [("2026-10-30", 100.0, 100.0, 99.0)])
    paper.create_order(data_dir, SYM, "buy", account_id="default", qty=100, order_type="next_open", ref_price=100.0)
    client.post("/api/paper/accounts/default/stop")

    summary = paper.settle_day(data_dir, "2026-10-30", account_id="default")
    assert summary["filled"] == 0 and summary["nav"] is None
    assert paper.load_orders(data_dir, "default")[0]["status"] == "pending"
    assert paper.load_nav(data_dir, "default") == []

    client.post("/api/paper/accounts/default/start")
    summary = paper.settle_day(data_dir, "2026-10-30", account_id="default")
    assert summary["filled"] == 1 and summary["nav"] is not None


def test_frozen_account_skips_evaluate_plans(client: TestClient, data_dir: Path):
    """冻结后计划既不触发买入也不推进状态 (不改 last_error / 不做过期)。"""
    plan = _make_plan(data_dir)
    client.post("/api/paper/accounts/default/stop")

    assert paper_plans.evaluate_plans(data_dir, {SYM: 100.0}, account_id="default") == []
    again = paper_plans.get_plan(data_dir, plan["id"], "default")
    assert again["status"] == "pending"
    assert again["last_error"] is None
    assert paper.load_orders(data_dir, "default") == []  # 没挂买单

    client.post("/api/paper/accounts/default/start")
    events = paper_plans.evaluate_plans(data_dir, {SYM: 100.0}, account_id="default")
    assert len(events) == 1
    assert paper_plans.get_plan(data_dir, plan["id"], "default")["status"] == "bought"


def test_frozen_account_rejects_new_order(client: TestClient, data_dir: Path):
    """create_order 是最后一道门禁: 冻结账户的下单请求直接拒。"""
    client.post("/api/paper/accounts/default/stop")
    order, err = paper.create_order(data_dir, SYM, "buy", account_id="default", qty=100, ref_price=100.0)
    assert order is None and err and "冻结" in err


# ── 重置 ────────────────────────────────────────────────
def _seed_account_activity(data_dir: Path) -> None:
    """造一份「有账可清」的账户: 成交 + 持仓 + 净值 + 计划 + 告警 + 跟单规则。"""
    from app.services import alert_store
    from app.strategy import paper_auto

    _write_daily(data_dir, [("2026-10-29", 100.0, 100.0, 99.0), ("2026-10-30", 100.0, 105.0, 99.0)])
    paper.create_order(data_dir, SYM, "buy", account_id="default", qty=100, ref_price=100.0)
    paper.settle_day(data_dir, "2026-10-29", account_id="default")
    paper.settle_day(data_dir, "2026-10-30", account_id="default")
    assert paper.load_positions(data_dir, "default")[SYM]["qty"] == 100
    assert paper.load_nav(data_dir, "default")
    _make_plan(data_dir, symbol="000001.SZ")
    paper_auto.create_auto_rule(
        data_dir,
        {"name": "测试跟单", "match_kind": "strategy", "match_id": "zdk_breakout",
         "side": "buy", "size_mode": "fixed_amount", "size_value": 10000.0},
        "default",
    )
    # ts 用当前毫秒 (alert_store.list_recent 按近 N 天过滤, 老时间戳读不出来)
    now_ms = int(datetime.now().timestamp() * 1000)
    alert_store.append_many(data_dir, [
        {"ts": now_ms, "source": "paper", "type": "paper_fill", "account_id": "default", "message": "成交"},
        {"ts": now_ms + 1, "source": "paper", "type": "paper_fill", "account_id": "other", "message": "别人的"},
    ])


def test_reset_clears_books_and_keeps_rules(client: TestClient, data_dir: Path):
    _seed_account_activity(data_dir)
    r = client.post("/api/paper/reset?confirm=true")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["account"]["cash"] == 1_000_000
    assert body["account"]["status"] == "active"
    cleared = body["cleared"]
    assert cleared["fills"] > 0 and cleared["positions"] == 1
    assert cleared["nav"] > 0 and cleared["plans"] == 1
    # 本账户的成交/计划告警都清掉 (含无 account_id 的老事件), 别人的保留
    assert cleared["alerts"] == 2
    assert cleared["auto_rules"] == 0  # keep_rules 默认 true, 不动规则

    # 账清了
    assert paper.load_fills(data_dir, "default") == []
    assert paper.load_orders(data_dir, "default") == []
    assert paper.load_nav(data_dir, "default") == []
    assert paper.overview(data_dir, account_id="default")["holdings"] == []
    assert paper.overview(data_dir, account_id="default")["cash"] == 1_000_000
    assert paper_plans.list_plans(data_dir, "default") == []
    # 规则与计划参数保留
    from app.strategy import paper_auto

    assert len(paper_auto.load_auto_rules(data_dir, "default")) == 1
    assert paper_plans.load_cfg(data_dir, "default")["risk_budget_pct"] == 1.0
    # 告警只清本账户的 (别人那条还在)
    from app.services import alert_store

    left = alert_store.list_recent(data_dir, days=3650)
    assert [ev["message"] for ev in left] == ["别人的"]


def test_reset_without_confirm_rejected(client: TestClient, data_dir: Path):
    _seed_account_activity(data_dir)
    r = client.post("/api/paper/reset")
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    # 数据原封不动
    assert paper.load_fills(data_dir, "default")
    assert paper_plans.list_plans(data_dir, "default")


def test_reset_keep_rules_false_wipes_rules(client: TestClient, data_dir: Path):
    _seed_account_activity(data_dir)
    r = client.post("/api/paper/reset?confirm=true&keep_rules=false")
    assert r.status_code == 200
    assert r.json()["cleared"]["auto_rules"] == 1
    from app.strategy import paper_auto

    assert paper_auto.load_auto_rules(data_dir, "default") == []
    # plan_config 也回到内置默认 (文件被删, load_cfg 回落默认)
    assert paper_plans.load_cfg(data_dir, "default")["risk_budget_pct"] == 1.0


def test_reset_reactivates_frozen_account(client: TestClient, data_dir: Path):
    client.post("/api/paper/accounts/default/stop")
    r = client.post("/api/paper/reset?confirm=true")
    assert r.status_code == 200 and r.json()["account"]["status"] == "active"


def test_reset_unknown_account_404(client: TestClient):
    assert client.post("/api/paper/reset?confirm=true&account=no_such").status_code == 404
    assert client.post("/api/paper/reset?confirm=true&account=../evil").status_code == 400


def test_reset_body_confirm_also_accepted(client: TestClient, data_dir: Path):
    """前端习惯把 confirm 放 body; 两种写法都认。"""
    r = client.post("/api/paper/reset", json={"confirm": True, "keep_rules": True})
    assert r.status_code == 200 and r.json()["ok"] is True


# ── plans/from_rows: 挑行建计划 (不重跑策略) ─────────────
def test_plans_from_rows_creates_selected(client: TestClient, data_dir: Path):
    rows = [
        {"symbol": SYM, "name": "贵州茅台", "close": 100.0, "low": 97.0, "atr_14": 2.0},
        {"symbol": "000001.SZ", "name": "平安银行", "close": 12.0, "low": 11.5, "atr_14": 0.3},
    ]
    r = client.post("/api/paper/plans/from_rows", json={"rows": rows})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["created"] == 2 and body["skipped"] == []
    assert body["source"] == "screener"
    syms = {p["symbol"] for p in body["plans"]}
    assert syms == {SYM, "000001.SZ"}
    for p in body["plans"]:
        assert p["status"] == "pending"
        assert p["entry_low"] < p["entry_high"]
        assert p["stop"] < p["entry_low"]
        assert p["qty_planned"] >= 100 and p["qty_planned"] % 100 == 0
    assert {p["symbol"] for p in paper_plans.list_plans(data_dir, "default")} == syms


def test_plans_from_rows_empty_rejected(client: TestClient):
    r = client.post("/api/paper/plans/from_rows", json={"rows": []})
    assert r.status_code == 400 and "未选择" in r.json()["detail"]


def test_plans_from_rows_caps_row_count(client: TestClient):
    rows = [{"symbol": f"6000{i:02d}.SH", "close": 10.0} for i in range(101)]
    r = client.post("/api/paper/plans/from_rows", json={"rows": rows})
    assert r.status_code == 400 and "最多" in r.json()["detail"]


def test_plans_from_rows_skips_active_symbol(client: TestClient, data_dir: Path):
    """同标的已有活跃计划 → 进 skipped, 不重复建单。"""
    _make_plan(data_dir)  # 已在 SYM 上有 pending 计划
    r = client.post("/api/paper/plans/from_rows", json={"rows": [
        {"symbol": SYM, "close": 100.0, "atr_14": 2.0},
        {"symbol": "000001.SZ", "close": 12.0},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert [s["symbol"] for s in body["skipped"]] == [SYM]
    assert "活跃计划" in body["skipped"][0]["reason"]


def test_plans_from_rows_skips_unaffordable(client: TestClient, data_dir: Path):
    """股价高到一手都买不起 → 进 skipped (股数不足一手)。"""
    paper.create_account(data_dir, 5_000, account_id="small", name="小账户")
    r = client.post("/api/paper/plans/from_rows?account_id=small", json={"rows": [{"symbol": SYM, "close": 1000.0}]})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 0
    assert [s["symbol"] for s in body["skipped"]] == [SYM]
    assert "一手" in body["skipped"][0]["reason"]


def test_plans_from_rows_uninitialized_account_400(client: TestClient):
    """权益为 0 的账户 → 400 (与 from_strategy 一致)。"""
    r = client.post("/api/paper/plans/from_rows?account_id=no_such", json={"rows": [{"symbol": SYM, "close": 100.0}]})
    assert r.status_code == 400 and "权益为 0" in r.json()["detail"]


def test_plans_from_rows_uses_cfg_override(client: TestClient, data_dir: Path):
    """cfg 覆盖只作用于本次调用, 不落盘。"""
    r = client.post("/api/paper/plans/from_rows", json={
        "rows": [{"symbol": SYM, "close": 100.0, "atr_14": 2.0}],
        "cfg": {"risk_budget_pct": 2.0},
    })
    assert r.status_code == 200
    assert paper_plans.load_cfg(data_dir, "default")["risk_budget_pct"] == 1.0  # 未落盘


def test_create_plans_from_rows_backward_compatible(data_dir: Path):
    """create_plans_from_rows 仍可按 (created, state) 解包, skipped 挂在 created 上。"""
    created, state = paper_plans.create_plans_from_rows(
        data_dir, [{"symbol": SYM, "close": 100.0}], top_n=10
    )
    assert isinstance(created, list) and len(created) == 1
    assert state is None
    assert list(created.skipped) == []
