"""自动跟单 0.3.43 新语义: 只数上限随大盘择时缩放 + 仓位「总仓均分」。

覆盖三条用户诉求 (m19308/m19313):
1. max_positions 不再是硬上限, 而是「乐观上限」, 乘「择时 × 情绪」后取整缩放;
2. position_mode=equal_total: 每票 = 建议总仓 ÷ 计划持仓只数 (不再固定每票 %);
3. 卖出路径完全不受缩放影响 (铁律: 卖出永不打折)。

口径与实现要点:
- 缩放后**不超过**用户设的上限 (round(5×1.2)=6 会越界, 必须夹回);
- 缩放后**至少 1 只** (弱市也不能缩到 0 而完全错过行情);
- 总仓系数 = 择时 × 情绪 × 外围风险×回撤, 绝不用 synthesize() 的合成 exposure;
- equal_total 的分母是**计划持仓只数**, 不是「已持有 + 1」(后者会让第一票
  吃掉 100% 权益, 且总投入超过建议总仓)。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

SYM = "600519.SH"
SYM2 = "000001.SZ"
SYM3 = "000002.SZ"
DAY = date(2026, 9, 24)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _market_session(monkeypatch):
    now = datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())


def _rule(**over) -> dict:
    base = {
        "name": "跟策略", "match_kind": "strategy", "match_id": "strat_1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10000,
        "order_type": "market", "cooldown_days": 0,
    }
    base.update(over)
    return base


def _ev(symbol: str, price: float = 10.0) -> dict:
    return {"source": "strategy", "strategy_id": "strat_1", "symbol": symbol, "price": price}


def _patch_coeffs(monkeypatch, timing: float, phase: float, radar: float = 1.0):
    """钉住择时/情绪/外围系数, 让缩放结果可预期。"""
    monkeypatch.setattr(
        paper_auto, "_auto_sizing_coeffs",
        lambda *a, **k: {"timing": timing, "phase": phase, "radar_adj": radar, "ok": True},
    )
    # _sizing_qty 的 equal_total 分支自己也算一遍系数 → 同步钉住底层来源
    monkeypatch.setattr(paper_auto._tp, "trend_exposure",
                        lambda *a, **k: {"state": "x", "label": "x", "exposure": timing, "ok": True})
    monkeypatch.setattr(paper_auto._tp, "exposure_multiplier", lambda *a, **k: phase)
    monkeypatch.setattr(
        paper_auto._tp, "latest_state", lambda *a, **k: {"state": "x", "date": "2026-09-24"})
    monkeypatch.setattr(
        "app.strategy.paper_plans._radar_coeffs",
        lambda *a, **k: {"ok": True, "global_exposure": radar, "equity_factor": 1.0},
    )


# ── ① 只数上限随择时/情绪缩放 ────────────────────────────────

def test_scaled_max_positions_shrinks_in_weak_market():
    """择时×情绪 = 0.5 时, 上限 5 → 缩到 3 (round(2.5)=2? 银行家舍入 → 2)。"""
    # 直接测缩放函数, 不依赖行情数据
    assert paper_auto._scaled_max_positions(5, {"timing": 0.4, "phase": 1.0}) == 2
    assert paper_auto._scaled_max_positions(5, {"timing": 1.0, "phase": 1.0}) == 5


def test_scaled_max_positions_never_exceeds_user_cap():
    """择时系数 >1 时不得反而多买: 上限 5 → 最多还是 5。"""
    assert paper_auto._scaled_max_positions(5, {"timing": 1.2, "phase": 1.2}) == 5
    assert paper_auto._scaled_max_positions(5, {"timing": 2.0, "phase": 2.0}) == 5


def test_scaled_max_positions_keeps_at_least_one():
    """弱市缩放后至少保留 1 只 (不能缩到 0 而完全错过行情)。"""
    assert paper_auto._scaled_max_positions(5, {"timing": 0.1, "phase": 0.1}) == 1
    assert paper_auto._scaled_max_positions(1, {"timing": 0.1, "phase": 0.1}) == 1


def test_scaled_max_positions_zero_means_unlimited():
    assert paper_auto._scaled_max_positions(0, {"timing": 0.5, "phase": 0.5}) == 0


def test_weak_timing_blocks_extra_positions(data_dir: Path, monkeypatch):
    """上限 5 但择时×情绪=0.4 → 缩到 2 只: 第 3 只信号被跳过。"""
    _patch_coeffs(monkeypatch, timing=0.4, phase=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=5))

    created = paper_auto.on_rule_events(
        data_dir, [_ev(SYM), _ev(SYM2, 12.0), _ev(SYM3, 13.0)])
    assert len(created) == 2
    assert {o["symbol"] for o in created} == {SYM, SYM2}


def test_strong_timing_allows_full_cap(data_dir: Path, monkeypatch):
    """择时×情绪=1.0 → 缩放后仍是 5, 5 只都能开。"""
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=5))

    events = [_ev(SYM), _ev(SYM2, 12.0), _ev(SYM3, 13.0)]
    assert len(paper_auto.on_rule_events(data_dir, events)) == 3


def test_scaling_does_not_block_add_to_held_symbol(data_dir: Path, monkeypatch):
    """已占满时, 加仓已持仓标的仍放行 (加仓不占新槽)。"""
    _patch_coeffs(monkeypatch, timing=0.4, phase=1.0)   # 上限缩到 2
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_positions=5))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 12.0)])
    assert len(created) == 2
    paper.evaluate_intraday(data_dir, {SYM: 10.0, SYM2: 12.0})
    # 再买已持仓的 SYM → 加仓放行
    assert len(paper_auto.on_rule_events(data_dir, [_ev(SYM)])) == 1


def test_scaling_never_limits_sell(data_dir: Path, monkeypatch):
    """铁律: 卖出永不打折 — 弱市缩放到 1 只时, 卖出照样全部放行。"""
    _patch_coeffs(monkeypatch, timing=0.1, phase=0.1)
    paper.create_account(data_dir, 1_000_000)
    for sym in (SYM, SYM2):
        order, err = paper.create_order(data_dir, sym, "buy", qty=1000, ref_price=10.0)
        assert err is None
    paper.evaluate_intraday(data_dir, {SYM: 10.0, SYM2: 10.0})

    next_day = datetime.combine(DAY + timedelta(days=1), time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: next_day)
    monkeypatch.setattr(paper, "cn_today", lambda: next_day.date())
    paper_auto.create_auto_rule(
        data_dir, _rule(side="sell", max_positions=5, size_value=10000))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM), _ev(SYM2, 10.0)])
    assert len(created) == 2 and all(o["side"] == "sell" for o in created)


# ── ② position_mode = 总仓均分 ────────────────────────────────

def test_equal_total_divides_suggested_total_by_planned_count(data_dir: Path, monkeypatch):
    """总仓均分: 建议总仓 100% (择时1.0×情绪1.0×外围1.0), 计划 4 只 → 每票 25%。

    权益 100 万 → 每票 25 万 → 价 10 元 → 25000 股。
    """
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0, radar=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=4,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert len(created) == 1
    assert created[0]["qty"] == 25_000   # 100万 × 100% ÷ 4 = 25万 → 2.5万股


def test_equal_total_first_ticket_does_not_take_all_equity(data_dir: Path, monkeypatch):
    """回归: 分母必须是「计划只数」而非「已持有+1」。

    旧实现下第一票会吃掉 100% 权益 (择时中性时), 后续每票越买越小。
    """
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0, radar=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=5,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert created[0]["qty"] == 20_000   # 100万 × 100% ÷ 5 = 20万 → 2万股 (不是 100 万股)


def test_equal_total_scales_down_with_timing(data_dir: Path, monkeypatch):
    """择时×情绪=0.5 → 建议总仓 50%, 计划只数 5 → 缩到 2 → 每票 25%。

    100 万 × 50% ÷ 2 = 25 万 → 价 10 元 → 25000 股。
    """
    _patch_coeffs(monkeypatch, timing=0.5, phase=1.0, radar=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=5,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert created[0]["qty"] == 25_000


def test_equal_total_uses_radar_coefficient(data_dir: Path, monkeypatch):
    """外围风险×回撤 = 0.5 → 建议总仓再打 5 折。"""
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0, radar=0.5)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=4,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    # 100万 × (100%×0.5) ÷ 4 = 12.5万 → 12500 股
    assert created[0]["qty"] == 12_500


def test_fixed_position_mode_keeps_old_behaviour(data_dir: Path, monkeypatch):
    """position_mode=fixed: 仍按 size_value 的每票固定比例, 不受择时缩放影响。"""
    _patch_coeffs(monkeypatch, timing=0.5, phase=1.0, radar=0.5)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=5,
        position_mode="fixed"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert created[0]["qty"] == 10_000   # 100万 × 10% = 10万 → 1万股


def test_missing_position_mode_defaults_to_fixed(tmp_path: Path):
    """存量规则文件缺该字段 → normalize 补 fixed, 不破坏老行为。"""
    rule = paper_auto.normalize_rule(_rule())
    assert rule["position_mode"] == "fixed"


def test_rule_validation_rejects_bad_position_mode(tmp_path: Path):
    with pytest.raises(ValueError, match="position_mode"):
        paper_auto.create_auto_rule(tmp_path, _rule(position_mode="whatever"))


def test_equal_total_without_max_positions_falls_back_to_slots(data_dir: Path, monkeypatch):
    """未设只数上限时: 不知道最终会买几只 → 用单票仓位上限 (max_pos_pct=20%) 兜底。

    旧实现按「已占槽 + 1」分母且不设单票上限 → 择时中性时第一票就吃掉 100% 权益,
    手续费还会让 create_order 的资金预检直接拒单 (明明有额度却一只没买)。
    现在: 每票 = min(建议总仓% ÷ 估分母, max_pos_pct) = min(100%, 20%) = 20%。
    """
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0, radar=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=0,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert len(created) == 1
    # 100万 × 20% = 20万 → 2万股 (单票仓位上限兜底, 不是 10 万股)
    assert created[0]["qty"] == 20_000


def test_equal_total_is_affordable_when_total_exceeds_cash(data_dir: Path, monkeypatch):
    """建议总仓 100% 且只数上限=1 → 仍要买得下去 (留手续费余量)。

    100 万权益 × 100% = 100 万, 但现金只有 100 万, 手续费+滑点会让资金预检拒单。
    修复后按可用现金 ×0.995 兜底 → 995,000 → 99500 股, 订单成立。
    """
    _patch_coeffs(monkeypatch, timing=1.0, phase=1.0, radar=1.0)
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(
        size_mode="pct_equity", size_value=10.0, max_positions=1,
        position_mode="equal_total"))

    created = paper_auto.on_rule_events(data_dir, [_ev(SYM)])
    assert len(created) == 1
    assert 0 < created[0]["qty"] <= 100_000
    assert created[0]["qty"] == 99_500   # 995,000 ÷ 10 元
