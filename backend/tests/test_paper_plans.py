"""模拟盘交易计划 (plan) 域模块测试 — 落盘 / 触发买入 / 止损止盈 / T+1 / 迁移。

口径: 计划只产出订单, 账务一律走 app/strategy/paper.py 的 create_order + 撮合,
因此本文件按真实调用顺序断言: evaluate_plans (挂单) → evaluate_intraday (成交)
→ evaluate_plans (状态对齐)。纯函数数学口径见 test_trade_plan.py。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_plans

SYM = "600519.SH"
DAY = date(2026, 11, 2)
DAY2 = DAY + timedelta(days=1)


@pytest.fixture(autouse=True)
def _neutral_radar(monkeypatch):
    """把风险雷达钉成中性系数 (1.0 × 1.0) 并清缓存。

    本文件关心的是计划流转本身, 不能让测试真的去联网取外围行情 (慢且不稳定);
    雷达与动态系数的行为在 test_paper_auto_exposure.py 里专门验证。
    v0.3.48: 顺带把「今日上限」钉成 100% (= 不限制), 使本文件原有买入断言
    不受新硬闸门影响; 闸门本身在 test_paper_cap_gate.py 里专门验证。
    """
    monkeypatch.setattr(
        paper_plans, "_radar_coeffs", lambda *a, **kw: {**paper_plans._NEUTRAL_RADAR}
    )
    monkeypatch.setattr(
        paper_plans, "suggested_exposure_pct", lambda *a, **kw: 100.0
    )
    paper_plans.invalidate_radar_cache()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """默认账户 + 时间冻结到 DAY (北京时间 10:00 盘中)。"""
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(
        paper_plans, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    )
    paper.create_account(tmp_path, 1_000_000)
    return tmp_path


def _advance_day(monkeypatch, day: date) -> None:
    """把「今天」推到 day (跨日后 T+1 可卖放行)。"""
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: day)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(day, time(10, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(
        paper_plans, "cn_now", lambda: datetime.combine(day, time(10, 0), tzinfo=CN_TZ)
    )


def _make_plan(tmp_path, **overrides) -> dict:
    payload = {
        "symbol": SYM,
        "name": "贵州茅台",
        "entry_low": 98.0,
        "entry_high": 102.0,
        "stop": 96.0,
        "qty_planned": 1000,
    }
    payload.update(overrides)
    return paper_plans.create_plan(tmp_path, payload)


def _write_daily(data_dir, rows: list[tuple[str, float, float]]) -> None:
    """写日线 (day, open, close) 到 kline_daily 分区 (settle_day 撮合 next_open 读它)。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(data_dir))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM] * len(rows),
        "date": [date.fromisoformat(r[0]) for r in rows],
        "open": [r[1] for r in rows],
        "close": [r[2] for r in rows],
        "low": [r[2] for r in rows],
    }))


# ── 建计划 / 读写 / 删除 ────────────────────────────────
def test_create_plan_derives_targets_and_persists(env):
    plan = _make_plan(env)
    assert plan["status"] == "pending"
    assert plan["entry_ref"] == pytest.approx(100.0)     # (98+102)/2
    assert plan["stop_pct"] == pytest.approx(4.0)
    assert plan["target1"] == pytest.approx(106.0)       # 100 + 1.5R
    assert plan["target2"] == pytest.approx(112.0)       # 100 + 3R
    assert plan["trail_stop"] == pytest.approx(96.0)
    assert plan["qty_planned"] == 1000
    assert plan["risk_amount"] == pytest.approx(4.0 * 1000)
    assert plan["gain_at_t1"] == pytest.approx(6000.0)
    assert plan["gain_at_t2"] == pytest.approx(12000.0)
    assert plan["t1_done"] is False
    # 落盘可回读 (中文不乱码)
    got = paper_plans.get_plan(env, plan["id"])
    assert got is not None and got["name"] == "贵州茅台" and got["status"] == "pending"


def test_create_plan_validation(env):
    with pytest.raises(ValueError, match="symbol"):
        paper_plans.create_plan(env, {"entry_low": 10.0, "entry_high": 11.0})
    with pytest.raises(ValueError, match="买入区间"):
        paper_plans.create_plan(env, {"symbol": SYM, "entry_low": 0, "entry_high": 11.0})
    with pytest.raises(ValueError, match="止损价"):
        paper_plans.create_plan(
            env, {"symbol": SYM, "entry_low": 10.0, "entry_high": 11.0, "stop": 20.0, "qty_planned": 100}
        )
    with pytest.raises(ValueError, match="不足一手"):
        # 权益折算后不足一百股 (高价标的 + 20% 仓位上限 = 0 手)
        paper_plans.create_plan(
            env,
            {"symbol": SYM, "entry_low": 999_000.0, "entry_high": 1_000_000.0, "qty_planned": 50},
        )


def test_create_plan_swaps_reversed_zone_and_defaults_qty_from_pos_pct(env):
    plan = paper_plans.create_plan(
        env, {"symbol": SYM, "entry_low": 102.0, "entry_high": 98.0, "pos_pct": 10.0}
    )
    assert plan["entry_low"] == pytest.approx(98.0)
    assert plan["entry_high"] == pytest.approx(102.0)
    # 1,000,000 * 10% / 100 = 1000 股
    assert plan["qty_planned"] == 1000
    assert plan["pos_pct"] == pytest.approx(10.0)
    # 未给止损 → 不推算目标价
    assert plan["stop"] is None and plan["target1"] is None


def test_list_get_delete_and_cancel_plan(env):
    a = _make_plan(env)
    b = _make_plan(env, symbol="000001.SZ", name="平安银行")
    assert len(paper_plans.list_plans(env)) == 2
    assert {p["id"] for p in paper_plans.list_plans(env)} == {a["id"], b["id"]}

    # status 过滤 (str 逗号分隔 / list)
    assert len(paper_plans.list_plans(env, status="pending")) == 2
    assert paper_plans.list_plans(env, status="holding") == []
    assert len(paper_plans.list_plans(env, status=["pending", "bought"])) == 2

    # 取消: 留档不改文件内容里的其它字段; 已取消再取消原样返回
    cancelled = paper_plans.cancel_plan(env, a["id"])
    assert cancelled["status"] == "cancelled"
    assert paper_plans.cancel_plan(env, a["id"])["status"] == "cancelled"
    assert len(paper_plans.list_plans(env, status="pending")) == 1

    # 删除: 物理删除; 再删返回 False
    assert paper_plans.delete_plan(env, b["id"]) is True
    assert paper_plans.get_plan(env, b["id"]) is None
    assert paper_plans.delete_plan(env, b["id"]) is False
    # 不存在 / 非 plan_ 前缀 → 拒绝 (防路径穿越)
    assert paper_plans.get_plan(env, "../account") is None
    assert paper_plans.cancel_plan(env, "nope") is None


def test_plan_dir_is_per_account(env):
    paper.create_account(env, 500_000, account_id="acc_a")
    mine = _make_plan(env)
    assert paper_plans.list_plans(env, "acc_a") == []
    assert [p["id"] for p in paper_plans.list_plans(env, "default")] == [mine["id"]]
    # 另一个账户看不到 / 删不掉
    assert paper_plans.get_plan(env, mine["id"], "acc_a") is None
    assert paper_plans.delete_plan(env, mine["id"], "acc_a") is False
    assert paper_plans.get_plan(env, mine["id"], "default") is not None


# ── 计划参数 cfg ────────────────────────────────────────
def test_cfg_roundtrip_and_range_clamp(env):
    base = paper_plans.load_cfg(env)
    assert base["risk_budget_pct"] == pytest.approx(1.0)

    saved = paper_plans.save_cfg(env, {"risk_budget_pct": 2.0, "max_hold_days": 5})
    assert saved["risk_budget_pct"] == pytest.approx(2.0)
    assert saved["max_hold_days"] == 5
    assert paper_plans.load_cfg(env)["risk_budget_pct"] == pytest.approx(2.0)

    # 越界值回落默认, 未知键丢弃
    saved = paper_plans.save_cfg(env, {"risk_budget_pct": 999, "bogus": 1})
    assert saved["risk_budget_pct"] == pytest.approx(1.0)
    assert "bogus" not in saved


def test_cfg_broken_file_falls_back_to_defaults(env):
    paper_plans.cfg_path(env).write_text("{not json", encoding="utf-8")
    assert paper_plans.load_cfg(env)["max_hold_days"] == 20


def test_labels_include_state_and_status_maps(env):
    labels = paper_plans.labels()
    assert labels["state_labels"]["weak"] == "弱势"
    assert labels["exposure"]["strong"] == pytest.approx(1.2)
    assert labels["stop_k"]["weak"] == pytest.approx(0.6)
    assert labels["status_labels"]["pending"] == "待触发"


# ── 触发买入 ────────────────────────────────────────────
def test_pending_plan_buys_when_price_enters_zone(env):
    plan = _make_plan(env)
    # 区间外 → 不挂单
    assert paper_plans.evaluate_plans(env, {SYM: 105.0}) == []
    assert paper_plans.get_plan(env, plan["id"])["status"] == "pending"

    # 进入区间 → 挂出市价买单 + 计划事件
    events = paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert len(events) == 1
    assert events[0]["type"] == "plan_buy"
    assert "交易计划买入" in events[0]["message"]
    orders = paper.load_orders(env)
    assert len(orders) == 1
    assert orders[0]["side"] == "buy" and orders[0]["qty"] == 1000
    assert orders[0]["source"] == f"plan:{plan['id']}"
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"

    # 同一轮重复轮询不会重复挂单
    assert paper_plans.evaluate_plans(env, {SYM: 100.0}) == []
    assert len(paper.load_orders(env)) == 1

    # 撮合成交 → 下一轮对齐为 holding, 回填成交均价 (含滑点与佣金摊薄)
    fills = paper.evaluate_intraday(env, {SYM: 100.0})
    assert len(fills) == 1
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding"
    assert got["qty_filled"] == 1000
    fill = paper.load_fills(env)[0]
    expected_avg = (1000 * fill["price"] + fill["fee"]) / 1000
    assert got["entry_price"] == pytest.approx(expected_avg, abs=1e-4)
    assert got["entry_price"] > fill["price"]                  # 佣金摊进成本


def test_plan_without_price_in_snapshot_is_untouched(env):
    plan = _make_plan(env)
    paper_plans.evaluate_plans(env, {"000002.SZ": 10.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "pending"


def test_plan_pending_expires_after_max_hold_days(env, monkeypatch):
    paper_plans.save_cfg(env, {"max_hold_days": 3})
    plan = _make_plan(env)
    paper_plans.evaluate_plans(env, {"000002.SZ": 10.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "pending"

    _advance_day(monkeypatch, DAY + timedelta(days=3))
    paper_plans.evaluate_plans(env, {"000002.SZ": 10.0})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "expired"
    assert "过期" in got["last_error"]


def test_plan_expires_when_buy_order_rejected(env):
    """买入单被撮合拒绝 (资金不足等) → 计划过期并带上拒单原因。"""
    paper.update_settings(env, "default", queue_limit_orders=False)
    paper_plans.save_cfg(env, {"max_hold_days": 20})
    plan = _make_plan(env, qty_planned=100)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    order = paper.load_orders(env)[0]
    assert order["status"] == "pending"

    # 把现金抽干后再撮合 → 拒单 expired
    acc = paper.get_account(env)
    acc["cash"] = 1.0
    paper.save_account(env, acc)
    paper.evaluate_intraday(env, {SYM: 100.0})
    assert paper.get_order(env, order["id"])["status"] == "expired"

    paper_plans.evaluate_plans(env, {SYM: 100.0})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "expired"
    assert "资金不足" in (got["last_error"] or "")


# ── 止损 / 止盈 ─────────────────────────────────────────
def _buy_and_hold(env, monkeypatch) -> dict:
    """建计划 → 触发买入 → 成交 → 对齐 holding, 返回计划。"""
    plan = _make_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding"
    _advance_day(monkeypatch, DAY2)   # 跨日 → T+1 可卖
    return got


def test_stop_loss_sells_all_and_closes_plan(env, monkeypatch):
    plan = _buy_and_hold(env, monkeypatch)
    assert paper_plans.get_plan(env, plan["id"])["stop"] == pytest.approx(96.0)

    events = paper_plans.evaluate_plans(env, {SYM: 95.0})
    assert len(events) == 1 and events[0]["type"] == "plan_sell"
    assert "触及止损" in events[0]["message"]
    sell = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sell) == 1 and sell[0]["qty"] == 1000

    paper.evaluate_intraday(env, {SYM: 95.0})
    paper_plans.evaluate_plans(env, {SYM: 95.0})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "closed"
    assert paper.load_positions(env)[SYM]["qty"] == 0


def test_target1_sells_half_and_raises_trail_to_breakeven(env, monkeypatch):
    plan = _buy_and_hold(env, monkeypatch)
    events = paper_plans.evaluate_plans(env, {SYM: 106.5})
    assert len(events) == 1
    assert "触及目标1" in events[0]["message"] and "减半" in events[0]["message"]

    sell = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sell) == 1 and sell[0]["qty"] == 500

    got = paper_plans.get_plan(env, plan["id"])
    assert got["t1_done"] is True
    # 移动止损抬到保本 (成交均价, 落盘按 2 位小数)
    assert got["trail_stop"] == pytest.approx(round(got["entry_price"], 2), abs=1e-9)
    assert got["trail_stop"] > got["stop"]

    paper.evaluate_intraday(env, {SYM: 106.5})
    paper_plans.evaluate_plans(env, {SYM: 106.5})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding" and got["qty_filled"] == 500
    # 已减半后不会重复减半
    assert paper_plans.evaluate_plans(env, {SYM: 106.5}) == []


def test_target2_clears_position(env, monkeypatch):
    plan = _buy_and_hold(env, monkeypatch)
    events = paper_plans.evaluate_plans(env, {SYM: 112.5})
    assert len(events) == 1 and "触及目标2" in events[0]["message"]
    sell = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sell) == 1 and sell[0]["qty"] == 1000

    paper.evaluate_intraday(env, {SYM: 112.5})
    paper_plans.evaluate_plans(env, {SYM: 112.5})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "closed"


def test_trailing_stop_exit_after_target1(env, monkeypatch):
    plan = _buy_and_hold(env, monkeypatch)
    paper_plans.evaluate_plans(env, {SYM: 106.5})          # 触发目标1, 抬移动止损
    paper.evaluate_intraday(env, {SYM: 106.5})
    paper_plans.evaluate_plans(env, {SYM: 106.5})
    got = paper_plans.get_plan(env, plan["id"])
    breakeven = got["trail_stop"]

    # 回落到保本价 → 移动止损清仓 (止损价 96 已不适用)
    events = paper_plans.evaluate_plans(env, {SYM: breakeven - 0.01})
    assert len(events) == 1 and "移动止损" in events[0]["message"]
    paper.evaluate_intraday(env, {SYM: breakeven - 0.01})
    paper_plans.evaluate_plans(env, {SYM: breakeven - 0.01})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "closed"


def test_sell_blocked_by_t1_leaves_trace_and_retries(env):
    """当日买入当日触发止损: T+1 不可卖 → 留痕且不挂单, 次日再试。"""
    plan = _make_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "holding"

    # 同一交易日跌到止损 → 无可卖数量
    assert paper_plans.evaluate_plans(env, {SYM: 95.0}) == []
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding"
    assert "T+1" in got["last_error"] and "可卖 0" in got["last_error"]
    assert [o for o in paper.load_orders(env) if o["side"] == "sell"] == []


# ── 买入执行方式 entry_mode ─────────────────────────────
def test_legacy_plan_without_entry_mode_defaults_to_zone(env):
    """老计划文件没有 entry_mode 字段 → 读取时视为 zone (进区间才买), 行为不变。"""
    import json

    plan = _make_plan(env)
    path = paper_plans.plans_dir(env) / f"{plan['id']}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("entry_mode", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    got = paper_plans.get_plan(env, plan["id"])
    assert got["entry_mode"] == "zone"
    # zone 口径: 区间外不挂单
    assert paper_plans.evaluate_plans(env, {SYM: 105.0}) == []
    assert paper_plans.get_plan(env, plan["id"])["status"] == "pending"


def test_plan_entry_mode_market_buys_without_waiting_zone(env):
    """market: 现价不在区间内也立即挂市价单, 当轮撮合成交。"""
    plan = _make_plan(env, entry_mode="market")
    assert plan["entry_mode"] == "market"

    events = paper_plans.evaluate_plans(env, {SYM: 105.0})    # 高于区间上沿 102
    assert len(events) == 1 and events[0]["type"] == "plan_buy"
    assert "立即市价" in events[0]["message"]

    order = paper.load_orders(env)[0]
    assert order["order_type"] == "market" and order["side"] == "buy"
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"

    paper.evaluate_intraday(env, {SYM: 105.0})
    paper_plans.evaluate_plans(env, {SYM: 105.0})     # 成交后对齐状态
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding" and got["qty_filled"] == 1000


def test_plan_entry_mode_next_open_buys_at_next_open(env, monkeypatch):
    """next_open: 不等区间, 挂次日开盘单, 由盘后 settle_day 按次日开盘价成交。"""
    plan = _make_plan(env, entry_mode="next_open")
    assert plan["entry_mode"] == "next_open"
    _write_daily(env, [(DAY.isoformat(), 100.0, 100.0), (DAY2.isoformat(), 103.0, 104.0)])

    events = paper_plans.evaluate_plans(env, {SYM: 105.0})
    assert len(events) == 1 and "次日开盘" in events[0]["message"]
    order = paper.load_orders(env)[0]
    assert order["order_type"] == "next_open" and order["status"] == "pending"
    # 盘中市价撮合不碰 next_open 单
    assert paper.evaluate_intraday(env, {SYM: 105.0}) == []
    assert paper.get_order(env, order["id"])["status"] == "pending"

    # 当日盘后: 下单时刻 (10:00) 晚于开盘价打印时刻 → 不按当日开盘价回填
    assert paper.settle_day(env, DAY.isoformat())["filled"] == 0
    assert paper.get_order(env, order["id"])["status"] == "pending"

    # 次一交易日盘后按当日 raw_open 成交 (含买入滑点 5bp)
    _advance_day(monkeypatch, DAY2)
    assert paper.settle_day(env, DAY2.isoformat())["filled"] == 1
    assert paper.get_order(env, order["id"])["status"] == "filled"
    fill = paper.load_fills(env)[0]
    assert fill["price"] == pytest.approx(103.0 * (1 + 5 / 10_000), abs=1e-6)

    paper_plans.evaluate_plans(env, {SYM: 103.5})
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "holding" and got["qty_filled"] == 1000
    assert got["entry_price"] > fill["price"]           # 佣金摊进成本


def test_create_plan_rejects_invalid_entry_mode(env):
    with pytest.raises(ValueError, match="entry_mode"):
        _make_plan(env, entry_mode="same_day")


def test_create_plan_uses_cfg_default_entry_mode(env):
    """不带 entry_mode → 用账户 cfg 的 default_entry_mode。"""
    assert _make_plan(env, symbol="600519.SH")["entry_mode"] == "zone"
    paper_plans.save_cfg(env, {"default_entry_mode": "next_open"})
    assert _make_plan(env, symbol="000001.SZ")["entry_mode"] == "next_open"
    # 显式入参优先于 cfg 默认
    assert _make_plan(env, symbol="600000.SH", entry_mode="market")["entry_mode"] == "market"


def test_create_plans_from_rows_entry_mode(env):
    rows = [{"symbol": "600519.SH", "close": 100.0, "atr_14": 2.0}]
    created, _ = paper_plans.create_plans_from_rows(env, rows, top_n=1)
    assert created[0]["entry_mode"] == "zone"

    created, _ = paper_plans.create_plans_from_rows(
        env, [{"symbol": "000001.SZ", "close": 10.0, "atr_14": 0.2}], top_n=1,
        entry_mode="market",
    )
    assert created[0]["entry_mode"] == "market"

    # cfg 默认值: 显式入参为 None 时生效
    paper_plans.save_cfg(env, {"default_entry_mode": "next_open"})
    created, _ = paper_plans.create_plans_from_rows(
        env, [{"symbol": "600000.SH", "close": 8.0, "atr_14": 0.2}], top_n=1
    )
    assert created[0]["entry_mode"] == "next_open"

    with pytest.raises(ValueError, match="entry_mode"):
        paper_plans.create_plans_from_rows(env, rows, top_n=1, entry_mode="bogus")


def test_labels_expose_entry_modes(env):
    labels = paper_plans.labels()
    assert labels["entry_modes"] == ["zone", "next_open", "market"]
    assert labels["default_entry_mode"] == "zone"
    assert labels["entry_mode_labels"]["zone"]


# ── 从策略行批量生成计划 ────────────────────────────────
def test_create_plans_from_rows_builds_and_dedups(env):
    rows = [
        {"symbol": "600519.SH", "close": 100.0, "atr_14": 2.0, "low": 97.0, "name": "贵州茅台"},
        {"symbol": "000001.SZ", "close": 10.0, "atr_14": 0.2, "low": 9.8, "name": "平安银行"},
    ]
    created, state = paper_plans.create_plans_from_rows(env, rows, top_n=10)
    assert state is None                       # 无 regime 表 → 无择时
    assert len(created) == 2
    assert all(p["status"] == "pending" and p["source"] == "strategy" for p in created)
    assert created[0]["qty_planned"] % 100 == 0 and created[0]["qty_planned"] >= 100

    # 已有活跃计划的标的跳过
    again, _ = paper_plans.create_plans_from_rows(env, rows, top_n=10)
    assert again == []
    assert len(paper_plans.list_plans(env)) == 2


def test_create_plans_from_rows_top_n_and_uninitialized_account(env):
    rows = [{"symbol": f"60000{i}.SH", "close": 10.0} for i in range(5)]
    created, _ = paper_plans.create_plans_from_rows(env, rows, top_n=2)
    assert len(created) == 2

    with pytest.raises(ValueError, match="权益"):
        paper_plans.create_plans_from_rows(env, rows, account_id="no_such_account")


def test_create_plans_from_rows_uses_regime_state(env):
    """有 regime 表时按情绪缩放仓位, 并把 state/exposure 记进计划。"""
    from app.services import regime_builder

    df = pl.DataFrame(
        {
            "date": [DAY],
            "state": ["weak"],
            "score": [10.0],
        },
        schema={"date": pl.Date, "state": pl.String, "score": pl.Float64},
    )
    regime_builder.upsert_regime_history(env, df)
    created, state = paper_plans.create_plans_from_rows(
        env, [{"symbol": "600519.SH", "close": 100.0, "atr_14": 2.0}], top_n=1
    )
    assert state is not None and state["state"] == "weak"
    assert created[0]["state"] == "weak"
    assert created[0]["exposure"] == pytest.approx(0.3)
    # weak: 止损收紧 0.6 → risk 4*0.6 = 2.4; 仓位再 *0.3
    assert created[0]["stop"] == pytest.approx(97.6)


# ── queue_limit_orders 存量迁移 ─────────────────────────
def test_migrate_queue_limit_defaults_is_idempotent(env):
    """存量账户 (显式 queue_limit_orders=False) 被一次性迁移为 True。"""
    paper.create_account(env, 100_000, account_id="arena_old1", queue_limit_orders=False)
    paper.create_account(env, 100_000, account_id="arena_old2", queue_limit_orders=False)
    assert paper.get_account(env, "arena_old1")["queue_limit_orders"] is False

    assert paper.migrate_queue_limit_defaults(env) == 2
    assert paper.get_account(env, "arena_old1")["queue_limit_orders"] is True
    assert paper.get_account(env, "arena_old2")["queue_limit_orders"] is True
    assert paper.get_account(env, "default")["queue_limit_orders"] is True

    # 幂等: 第二次调用 (标记文件已存在) 返回 0, 也不覆盖用户显式关闭
    paper.update_settings(env, "arena_old1", queue_limit_orders=False)
    assert paper.migrate_queue_limit_defaults(env) == 0
    assert paper.get_account(env, "arena_old1")["queue_limit_orders"] is False


def test_new_account_defaults_to_queue_limit_on(env):
    acc = paper.create_account(env, 100_000, account_id="fresh")
    assert acc["queue_limit_orders"] is True


def test_migrate_queue_limit_no_accounts_marks_done(env):
    """空数据目录: 返回 0 且写标记 (后续新建账户走新默认值)。"""
    assert paper.migrate_queue_limit_defaults(env) == 0
    assert (env / paper.QUEUE_LIMIT_MIGRATION_FLAG).exists()
    assert paper.migrate_queue_limit_defaults(env) == 0


# ── HTTP 契约 (裸 FastAPI, 只挂 paper 路由) ─────────────
@pytest.fixture
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.paper import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=env),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def test_api_plan_crud(client):
    body = {
        "symbol": SYM, "name": "贵州茅台", "entry_low": 98.0, "entry_high": 102.0,
        "stop": 96.0, "qty_planned": 1000,
    }
    r = client.post("/api/paper/plans?account_id=default", json=body)
    assert r.status_code == 200, r.text
    plan_id = r.json()["plan"]["id"]
    assert r.json()["plan"]["status"] == "pending"

    listed = client.get("/api/paper/plans?account_id=default").json()
    assert listed["total"] == 1 and listed["plans"][0]["id"] == plan_id
    # status 过滤
    assert client.get("/api/paper/plans?account_id=default&status=pending").json()["total"] == 1
    assert client.get("/api/paper/plans?account_id=default&status=holding").json()["total"] == 0

    # 缺必填字段 → 422 (pydantic); 业务非法 (止损高于区间上沿) → 400
    assert client.post("/api/paper/plans", json={"symbol": SYM}).status_code == 422
    bad = dict(body, stop=200.0)
    r = client.post("/api/paper/plans", json=bad)
    assert r.status_code == 400 and "止损价" in r.json()["detail"]

    # 取消 → 留档
    r = client.post(f"/api/paper/plans/{plan_id}/cancel?account_id=default")
    assert r.status_code == 200 and r.json()["plan"]["status"] == "cancelled"

    # 删除 → 404 再删
    assert client.delete(f"/api/paper/plans/{plan_id}?account_id=default").status_code == 200
    assert client.delete(f"/api/paper/plans/{plan_id}?account_id=default").status_code == 404
    assert client.post(f"/api/paper/plans/{plan_id}/cancel?account_id=default").status_code == 404


def test_api_plan_account_isolation_and_legacy_query_alias(client):
    """plan 路由按 ?account_id= 契约, 同时兼容旧 ?account=。"""
    client.post("/api/paper/account", json={"initial_cash": 1_000_000, "account_id": "acc_b"})
    body = {"symbol": SYM, "entry_low": 98.0, "entry_high": 102.0, "qty_planned": 100}
    client.post("/api/paper/plans?account_id=acc_b", json=body)
    assert client.get("/api/paper/plans?account_id=acc_b").json()["total"] == 1
    assert client.get("/api/paper/plans?account_id=default").json()["total"] == 0
    # 旧参数名 account 也可用 (同一账户)
    assert client.get("/api/paper/plans?account=acc_b").json()["total"] == 1
    # 非法账户 id → 400
    assert client.get("/api/paper/plans?account_id=../evil").status_code == 400


def test_api_plan_config_roundtrip(client):
    r = client.get("/api/paper/plan_config?account_id=default")
    assert r.status_code == 200
    payload = r.json()
    assert payload["cfg"]["risk_budget_pct"] == pytest.approx(1.0)
    assert payload["labels"]["status_labels"]["pending"] == "待触发"
    assert payload["state"] is None                       # 无 regime 表

    r = client.put("/api/paper/plan_config?account_id=default", json={"cfg": {"risk_budget_pct": 2.0}})
    assert r.status_code == 200 and r.json()["cfg"]["risk_budget_pct"] == pytest.approx(2.0)
    assert client.get("/api/paper/plan_config?account_id=default").json()["cfg"]["risk_budget_pct"] == pytest.approx(2.0)


def test_api_plans_from_strategy_without_engine_returns_503(client):
    r = client.post("/api/paper/plans/from_strategy", json={"strategy_id": "demo"})
    assert r.status_code == 503 and "策略引擎" in r.json()["detail"]


def test_api_plan_entry_mode_and_config(client):
    """entry_mode 三处入口: 建计划 / 改配置默认值; 非法值一律 400。"""
    body = {
        "symbol": SYM, "entry_low": 98.0, "entry_high": 102.0,
        "stop": 96.0, "qty_planned": 1000,
    }
    # 缺省 → zone (cfg 默认)
    r = client.post("/api/paper/plans?account_id=default", json=body)
    assert r.status_code == 200 and r.json()["plan"]["entry_mode"] == "zone"
    client.delete(f"/api/paper/plans/{r.json()['plan']['id']}?account_id=default")

    # 显式 market → 落盘
    r = client.post("/api/paper/plans?account_id=default", json=dict(body, entry_mode="market"))
    assert r.status_code == 200 and r.json()["plan"]["entry_mode"] == "market"
    client.delete(f"/api/paper/plans/{r.json()['plan']['id']}?account_id=default")

    # 非法值 → 400 (文案列出合法值)
    r = client.post("/api/paper/plans?account_id=default", json=dict(body, entry_mode="same_day"))
    assert r.status_code == 400 and "entry_mode" in r.json()["detail"]
    assert "zone" in r.json()["detail"]

    # cfg 读写 default_entry_mode
    r = client.put("/api/paper/plan_config?account_id=default", json={"cfg": {"default_entry_mode": "next_open"}})
    assert r.status_code == 200 and r.json()["cfg"]["default_entry_mode"] == "next_open"
    assert client.get("/api/paper/plan_config?account_id=default").json()["cfg"]["default_entry_mode"] == "next_open"
    # 之后建计划默认取 cfg
    r = client.post("/api/paper/plans?account_id=default", json=body)
    assert r.status_code == 200 and r.json()["plan"]["entry_mode"] == "next_open"
    # 非法配置值静默回落默认 (不炸 500)
    r = client.put("/api/paper/plan_config?account_id=default", json={"cfg": {"default_entry_mode": "bogus"}})
    assert r.status_code == 200 and r.json()["cfg"]["default_entry_mode"] == "zone"


def test_api_plans_from_rows_entry_mode(client):
    rows = [{"symbol": SYM, "close": 100.0, "atr_14": 2.0}]
    r = client.post("/api/paper/plans/from_rows?account_id=default", json={"rows": rows})
    assert r.status_code == 200, r.text
    assert r.json()["entry_mode"] == "zone"
    assert r.json()["plans"][0]["entry_mode"] == "zone"

    r = client.post(
        "/api/paper/plans/from_rows?account_id=default",
        json={"rows": [{"symbol": "000001.SZ", "close": 10.0}], "entry_mode": "market"},
    )
    assert r.status_code == 200 and r.json()["entry_mode"] == "market"

    r = client.post(
        "/api/paper/plans/from_rows?account_id=default",
        json={"rows": rows, "entry_mode": "same_day"},
    )
    assert r.status_code == 400 and "entry_mode" in r.json()["detail"]
