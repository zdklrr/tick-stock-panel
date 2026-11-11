"""v0.3.48 「今日上限 = 买入硬闸门」专项测试 (m28306 / ask_user_question 决策)。

口径:
  - 上限 = clamp(择时 × 情绪 × 外围, 0.15, 1.20) × 100, 与决策卡
    `/api/risk/advice.suggested_exposure_pct` 同源, 但买入热路径绝不调
    `synthesize()` (避免 NAS 上的联网自愈 120s 超时);
  - 买前判断「买完后 持仓市值 / 总资产」, 超了这一笔不买 (不缩量、不拒单报错、
    计划留 pending 下轮再试), 且写 last_error / 日志说明原因;
  - **铁律: 卖出永不受闸门影响**, 已有持仓一股不卖;
  - 只作用于「自动跟单 + 计划驱动」两条自动买入路径; 手动下单 (paper.create_order)
    不受限 (本文件不测它, 但闸门只在 on_rule_events / _try_buy 里调用)。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto, paper_plans

SYM = "600519.SH"
SYM2 = "000002.SZ"
DAY = date(2026, 11, 2)
DAY2 = DAY + timedelta(days=1)


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例都清掉 cap 的 60s timing 缓存与雷达缓存, 避免跨用例串味。"""
    paper_plans._TIMING_CACHE.clear()
    paper_plans.invalidate_radar_cache()
    yield
    paper_plans._TIMING_CACHE.clear()
    paper_plans.invalidate_radar_cache()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(
        paper_plans, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    )
    monkeypatch.setattr(
        paper_plans, "_radar_coeffs", lambda *a, **kw: {**paper_plans._NEUTRAL_RADAR}
    )
    paper.create_account(tmp_path, 1_000_000)
    return tmp_path


def _make_plan(tmp_path: Path, **overrides) -> dict:
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


def _rule(**over) -> dict:
    base = {
        "name": "跟策略", "match_kind": "strategy", "match_id": "strat_1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10000,
        "order_type": "market", "cooldown_days": 0, "enabled": True,
    }
    base.update(over)
    return base


def _ev(symbol: str, price: float = 10.0) -> dict:
    return {"source": "strategy", "strategy_id": "strat_1", "symbol": symbol, "price": price}


# ── ① suggested_exposure_pct 纯函数口径 ────────────────────────

def test_suggested_exposure_pct_matches_synthesize_formula(env):
    """三路系数相乘 + 夹到 [15%, 120%]; 传入系数时不再读盘/联网。"""
    assert paper_plans.suggested_exposure_pct(
        env, "default", timing=0.35, phase=1.0, risk=1.043
    ) == 36.5
    assert paper_plans.suggested_exposure_pct(
        env, "default", timing=1.0, phase=1.2, risk=1.1
    ) == 120.0  # clamp 上限
    assert paper_plans.suggested_exposure_pct(
        env, "default", timing=0.1, phase=0.1, risk=0.5
    ) == 15.0   # clamp 下限


def test_suggested_exposure_pct_uses_cached_timing_zero_network(monkeypatch, env):
    """不传系数时: 用 trend_timing(_heal=False) 只读本地, 60s 内复用缓存;
    绝不调 synthesize() / trend_timing(_heal=True)。"""
    import app.services.market_timing as mt

    calls = {"heal_false": 0, "synthesize": 0}

    def fake_trend_timing(*a, **kw):
        calls["heal_false"] += 1
        assert kw.get("_heal") is False, "硬闸门绝不允许带网络自愈的 trend_timing"
        return {"ok": True, "exposure": 0.35}

    def forbidden_synthesize(*a, **kw):
        calls["synthesize"] += 1
        raise AssertionError("买入热路径不应调用 synthesize()")

    monkeypatch.setattr(mt, "trend_timing", fake_trend_timing)
    monkeypatch.setattr(mt, "synthesize", forbidden_synthesize)
    monkeypatch.setattr(paper_plans, "_radar_coeffs",
                        lambda *a, **kw: {"ok": True, "global_exposure": 1.043, "equity_factor": 1.0})
    monkeypatch.setattr(paper_plans.trade_plan, "exposure_multiplier", lambda *a, **kw: 1.0)

    a = paper_plans.suggested_exposure_pct(env, "default")
    b = paper_plans.suggested_exposure_pct(env, "default")
    assert a == b == 36.5
    assert calls == {"heal_false": 1, "synthesize": 0}  # 第二次命中缓存


def test_suggested_exposure_pct_degrades_to_100_on_failure(monkeypatch, env):
    """任何一路取数异常 → 100% (不限制), 绝不抛异常卡住买入。"""
    monkeypatch.setattr(paper_plans, "_cap_timing", lambda *a, **kw: 1.0)
    monkeypatch.setattr(paper_plans.trade_plan, "exposure_multiplier",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert paper_plans.suggested_exposure_pct(env, "default") == 100.0


# ── ② 计划驱动买入路径 ────────────────────────────────────────

def test_plan_buy_blocked_when_over_cap(env, monkeypatch):
    """上限 30% + 已有 40% 仓位 → 新计划这一笔不买, 计划留 pending 并写原因。"""
    # 已有持仓: 4000 股 × 100 = 40 万市值, 现金 60 万, 总资产 100 万 → 40%
    order, err = paper.create_order(env, SYM2, "buy", qty=4000, ref_price=100.0)
    assert err is None
    assert len(paper.evaluate_intraday(env, {SYM2: 100.0})) == 1

    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 30.0)
    _make_plan(env)   # 1000 股 × 100 = 10 万, 买完到 50% > 30%

    events = paper_plans.evaluate_plans(env, {SYM: 100.0, SYM2: 100.0})
    assert events == []
    # 注意: order 是 create_order 返回的「下单时快照」, 撮合后落盘对象会补上
    # status='filled' / fill_price / fees, 不能按对象相等比较, 只断言没有新买单。
    assert len(paper.load_orders(env)) == 1
    got = paper_plans.list_plans(env)[0]
    assert got["status"] == "pending"          # 留 pending, 不缩量、不改状态
    assert "今日上限 30.0%" in (got["last_error"] or "")
    assert "已有持仓不受影响" in (got["last_error"] or "")


def test_plan_buy_allowed_when_under_cap(env, monkeypatch):
    """上限 100% (不限制) → 正常买入, 与旧行为一致。"""
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 100.0)
    plan = _make_plan(env)
    events = paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert len(events) == 1 and events[0]["type"] == "plan_buy"
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"
    assert len(paper.load_orders(env)) == 1


def test_plan_buy_recovers_after_position_drops(env, monkeypatch):
    """仓位降回上限以下后, 同一个 pending 计划下轮自动恢复买入。"""
    order, err = paper.create_order(env, SYM2, "buy", qty=4000, ref_price=100.0)
    assert err is None
    paper.evaluate_intraday(env, {SYM2: 100.0})
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 30.0)
    _make_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0, SYM2: 100.0})
    assert paper_plans.list_plans(env)[0]["status"] == "pending"

    # 卖掉 SYM2 → 仓位归 0 (卖出不受闸门影响, 铁律)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY2)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY2)
    paper._materialize(env)
    sold, err = paper.create_order(env, SYM2, "sell", qty=4000, ref_price=100.0)
    assert err is None
    paper.evaluate_intraday(env, {SYM2: 100.0})

    events = paper_plans.evaluate_plans(env, {SYM: 100.0, SYM2: 100.0})
    assert len(events) == 1 and events[0]["type"] == "plan_buy"
    assert paper_plans.list_plans(env)[0]["status"] == "bought"


def test_cap_gate_never_blocks_sells(env, monkeypatch):
    """铁律: 上限 15% 时止损/目标卖出照样全部放行, 一股不拦。"""
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 100.0)
    plan = _make_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "holding"

    # 跨日 T+1 可卖 + 把上限压到 15% (当前仓位仍 ~10%, 但卖出不看这个数)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY2)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY2)
    paper._materialize(env)
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 15.0)

    events = paper_plans.evaluate_plans(env, {SYM: 95.0})  # 跌破止损 96
    assert len(events) == 1 and events[0]["type"] == "plan_sell"
    sells = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == 1000


# ── ③ 自动跟单买入路径 ────────────────────────────────────────

def test_auto_buy_blocked_when_over_cap(env, monkeypatch):
    """上限 30% + 已有 40% 仓位 → 自动跟单这一笔跳过, 留日志不报错。"""
    # 4000 股 × 100 = 40 万市值, 总资产 100 万 → 40% > 30%
    order, err = paper.create_order(env, SYM2, "buy", qty=4000, ref_price=100.0)
    assert err is None
    paper.evaluate_intraday(env, {SYM2: 100.0})
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 30.0)
    paper_auto.create_auto_rule(env, _rule())

    # 事件快照只含 SYM, 已有持仓 SYM2 会按成本价 (100) 兜底估值 → 40% > 30%
    created = paper_auto.on_rule_events(env, [_ev(SYM, 100.0)])
    assert created == []
    assert len(paper.load_orders(env)) == 1  # 只有最初那笔买单, 撮合后对象会变 filled


def test_auto_buy_allowed_when_under_cap(env, monkeypatch):
    """上限 100% → 自动跟单正常下单。"""
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 100.0)
    paper_auto.create_auto_rule(env, _rule())
    created = paper_auto.on_rule_events(env, [_ev(SYM, 10.0)])
    assert len(created) == 1 and created[0]["side"] == "buy"


def test_cap_gate_never_blocks_auto_sells(env, monkeypatch):
    """铁律: 上限 15% 时自动卖出路径完全不碰闸门, 照样清仓。"""
    monkeypatch.setattr(paper, "cn_today", lambda: DAY - timedelta(days=1))
    paper.create_order(env, SYM, "buy", qty=500, ref_price=10.0)
    paper.evaluate_intraday(env, {SYM: 10.0})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper._materialize(env)
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 15.0)
    paper_auto.create_auto_rule(env, _rule(
        side="sell", size_mode="fixed_amount", size_value=10_000_000.0))

    created = paper_auto.on_rule_events(env, [_ev(SYM, 12.0)])
    assert len(created) == 1 and created[0]["side"] == "sell"


def test_cap_gate_tolerates_dirty_event_prices(env, monkeypatch):
    """脏事件 (price 坏字符串) 不能把整批自动跟单带崩。"""
    monkeypatch.setattr(paper_plans, "suggested_exposure_pct", lambda *a, **kw: 100.0)
    paper_auto.create_auto_rule(env, _rule())
    bad = {"source": "strategy", "strategy_id": "strat_1", "symbol": SYM2, "price": "N/A"}
    created = paper_auto.on_rule_events(env, [bad, _ev(SYM, 10.0)])
    assert len(created) == 1 and created[0]["symbol"] == SYM
