"""交易计划纯函数测试 — 止损/止盈/仓位/大盘情绪缩放的数学口径。

设计: docs/paper-trading-plan.md 的「交易计划」章节。本文件只测 app/strategy/trade_plan.py
(纯函数, 不碰磁盘/订单); 落盘与撮合由 test_paper_plans.py 覆盖。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.strategy import trade_plan


# ── 参数合并与校验 ──────────────────────────────────────
def test_merge_cfg_defaults_and_overrides():
    base = trade_plan.merge_cfg(None)
    assert base["risk_budget_pct"] == 1.0
    assert base["atr_stop_mult"] == 2.0
    assert base["regime_scale_enabled"] is True

    merged = trade_plan.merge_cfg({"risk_budget_pct": 2.5, "max_pos_pct": 30})
    assert merged["risk_budget_pct"] == 2.5
    assert merged["max_pos_pct"] == 30.0
    # 未覆盖的键保留默认
    assert merged["atr_stop_mult"] == 2.0


def test_merge_cfg_rejects_out_of_range_and_unknown_keys():
    """越界值回落默认 (脏配置不能炸掉计划), 未知键丢弃。"""
    cfg = trade_plan.merge_cfg({
        "risk_budget_pct": 999,      # > 10 上限
        "atr_stop_mult": -1,         # 低于下限
        "entry_zone_pct": "abc",     # 非数值
        "max_hold_days": 10**9,      # 超上限
        "no_such_key": 123,
    })
    assert cfg["risk_budget_pct"] == 1.0
    assert cfg["atr_stop_mult"] == 2.0
    assert cfg["entry_zone_pct"] == 2.0
    assert cfg["max_hold_days"] == 20
    assert "no_such_key" not in cfg


def test_merge_cfg_relative_constraints():
    """min_pos_pct 不得高于 max_pos_pct; target2 必须大于 target1。"""
    cfg = trade_plan.merge_cfg({"min_pos_pct": 30.0, "max_pos_pct": 10.0})
    assert cfg["min_pos_pct"] <= cfg["max_pos_pct"]

    cfg = trade_plan.merge_cfg({"target1_r": 3.0, "target2_r": 1.0})
    assert cfg["target2_r"] > cfg["target1_r"]


def test_merge_cfg_bool_switch_off():
    cfg = trade_plan.merge_cfg({
        "regime_scale_enabled": False,
        "regime_stop_tighten_enabled": False,
    })
    assert cfg["regime_scale_enabled"] is False
    assert cfg["regime_stop_tighten_enabled"] is False


# ── 大盘情绪 → 仓位倍率 / 止损收紧 ──────────────────────
def test_exposure_multiplier_by_state():
    assert trade_plan.exposure_multiplier("strong") == pytest.approx(1.2)
    assert trade_plan.exposure_multiplier("range") == pytest.approx(0.75)
    assert trade_plan.exposure_multiplier("weak") == pytest.approx(0.3)
    # 缺失/未知 state → 无择时
    assert trade_plan.exposure_multiplier(None) == 1.0
    assert trade_plan.exposure_multiplier("flying") == 1.0
    # dict 形式 (regime 端点返回结构)
    assert trade_plan.exposure_multiplier({"state": "weak", "score": 10}) == pytest.approx(0.3)


def test_exposure_multiplier_switch_off():
    assert trade_plan.exposure_multiplier("weak", {"regime_scale_enabled": False}) == 1.0


def test_stop_tighten_k_by_state():
    assert trade_plan.stop_tighten_k("strong") == pytest.approx(1.0)
    assert trade_plan.stop_tighten_k("lean_weak") == pytest.approx(0.75)
    assert trade_plan.stop_tighten_k("weak") == pytest.approx(0.6)
    assert trade_plan.stop_tighten_k(None) == 1.0
    assert trade_plan.stop_tighten_k("weak", {"regime_stop_tighten_enabled": False}) == 1.0


def test_state_key_and_label():
    assert trade_plan.state_key("strong") == "strong"
    assert trade_plan.state_key({"state": " range "}) == "range"
    assert trade_plan.state_key({"state": "??"}) is None
    assert trade_plan.state_key(None) is None
    # 中文标签来自 regime_builder.STATE_LABELS
    assert trade_plan.state_label("weak") == "弱势"
    assert trade_plan.state_label(None) is None


# ── build_plan: 止损/仓位/止盈 ──────────────────────────
def test_build_plan_atr_stop_and_position_from_risk_budget():
    """止损 = entry - 2*ATR; 仓位由风险预算反推 (亏损正好 1% 权益)。"""
    plan = trade_plan.build_plan(symbol="600519.SH", price=100.0, equity=1_000_000, atr=2.0)
    assert plan is not None
    assert plan["entry_ref"] == 100.0
    assert plan["stop"] == pytest.approx(96.0)          # 100 - 2*2
    assert plan["stop_pct"] == pytest.approx(4.0)
    # pos_pct = 1.0 / 4.0 * 100 = 25 → 超过 max_pos_pct 20 被夹
    assert plan["pos_pct"] == pytest.approx(20.0)
    assert plan["capped"] is True
    assert plan["qty_planned"] == 2000                  # 100w * 20% / 100 = 2000 股
    # 止盈按 R 倍数
    assert plan["target1"] == pytest.approx(106.0)      # 100 + 1.5*4
    assert plan["target2"] == pytest.approx(112.0)      # 100 + 3.0*4
    assert plan["rr"] == pytest.approx(1.5)
    assert plan["atr_estimated"] is False


def test_build_plan_risk_amount_matches_risk_budget():
    """止损被打掉时亏损 = 权益 * risk_budget_pct%。"""
    cfg = {"risk_budget_pct": 0.5, "max_pos_pct": 100.0}
    plan = trade_plan.build_plan(
        symbol="000001.SZ", price=10.0, equity=500_000, atr=0.2, cfg=cfg,
    )
    assert plan is not None
    assert plan["loss_at_stop"] == pytest.approx(plan["risk_amount"])
    # 2*0.2=0.4 → stop_pct 4% → pos_pct = 0.5/4*100 = 12.5%
    assert plan["pos_pct"] == pytest.approx(12.5)
    assert plan["qty_planned"] == 6200                  # 50w*12.5%/10 = 6250 → 取整百 6200
    # 实际风险敞口 ≈ 0.5% 权益 (取整百导致略小)
    assert plan["risk_amount"] <= 500_000 * 0.005 + 1e-6


def test_build_plan_swing_low_preferred_when_in_range():
    """前低有效 (距离落在 [1,2]*ATR 内) → 用结构位做止损。"""
    plan = trade_plan.build_plan(
        symbol="600000.SH", price=100.0, equity=1_000_000, atr=2.0, swing_low=97.0,
    )
    assert plan is not None
    assert plan["stop"] == pytest.approx(97.0)          # 距离 3 落在 [2,4] → 用前低


def test_build_plan_swing_low_ignored_when_out_of_range():
    """前低太近 (< 1*ATR) 或太远 (> mult*ATR) 时不采用, 回到波动率止损。"""
    near = trade_plan.build_plan(symbol="A", price=100.0, equity=1_000_000, atr=2.0, swing_low=99.5)
    assert near is not None and near["stop"] == pytest.approx(96.0)

    far = trade_plan.build_plan(symbol="B", price=100.0, equity=1_000_000, atr=2.0, swing_low=50.0)
    assert far is not None and far["stop"] == pytest.approx(96.0)


def test_build_plan_stop_capped_by_max_stop_pct():
    """ATR 过大时止损距离被 max_stop_pct 夹住 (不许一开仓就 -20%)。"""
    plan = trade_plan.build_plan(
        symbol="600519.SH", price=100.0, equity=1_000_000, atr=20.0,
        cfg={"max_stop_pct": 12.0, "max_pos_pct": 100.0},
    )
    assert plan is not None
    assert plan["stop"] == pytest.approx(88.0)
    assert plan["stop_pct"] == pytest.approx(12.0)


def test_build_plan_no_atr_fallback_3pct():
    """策略没有 ATR 列时用 3% 波动率兜底, 并标记 atr_estimated。"""
    plan = trade_plan.build_plan(symbol="600519.SH", price=100.0, equity=1_000_000)
    assert plan is not None
    assert plan["atr_estimated"] is True
    assert plan["atr"] == pytest.approx(3.0)
    assert plan["atr_pct"] == pytest.approx(3.0)
    assert plan["stop"] == pytest.approx(94.0)          # 100 - 2*3


def test_build_plan_entry_zone():
    plan = trade_plan.build_plan(symbol="X", price=100.0, equity=1_000_000, atr=1.0,
                                 cfg={"entry_zone_pct": 2.0})
    assert plan is not None
    assert plan["entry_low"] == pytest.approx(98.0)
    assert plan["entry_high"] == pytest.approx(102.0)
    assert plan["entry_low"] <= plan["entry_ref"] <= plan["entry_high"]


def test_build_plan_regime_scales_position_only():
    """大盘情绪缩放仓位, 不缩放止盈 (卖出保护不打折)。"""
    kw = {"symbol": "600519.SH", "price": 100.0, "equity": 10_000_000, "atr": 1.0}
    strong = trade_plan.build_plan(**kw, state="strong")
    weak = trade_plan.build_plan(**kw, state="weak")
    assert strong is not None and weak is not None
    # 2*1 / 100 = 2% → pos_pct 1/2*100 = 50 → 夹到 20 → *1.2 / *0.3(再乘止损收紧)
    assert strong["exposure"] == pytest.approx(1.2)
    assert weak["exposure"] == pytest.approx(0.3)
    assert strong["pos_pct"] > weak["pos_pct"]
    # 弱势市场止损收紧: risk 更小 → stop 更高
    assert weak["stop"] > strong["stop"]
    # 目标价只由 entry/stop 与 R 倍数决定, 不因情绪改变倍数口径
    assert strong["rr"] == weak["rr"] == pytest.approx(1.5)


def test_build_plan_returns_none_on_bad_input():
    assert trade_plan.build_plan(symbol="", price=10.0, equity=1e6) is None
    assert trade_plan.build_plan(symbol="A", price=0, equity=1e6) is None
    assert trade_plan.build_plan(symbol="A", price=float("nan"), equity=1e6) is None
    assert trade_plan.build_plan(symbol="A", price=10.0, equity=None) is None
    assert trade_plan.build_plan(symbol="A", price=10.0, equity=0) is None
    # 权益太小买不到一手
    assert trade_plan.build_plan(symbol="A", price=1000.0, equity=10_000, atr=1.0) is None


def test_build_plan_amounts_are_positive():
    """gain/loss 一律正数 (前端自己加 +- 号), 与 zdk 口径一致。"""
    plan = trade_plan.build_plan(symbol="A", price=50.0, equity=1_000_000, atr=1.0)
    assert plan is not None
    for key in ("risk_amount", "gain_at_t1", "gain_at_t2", "loss_at_stop"):
        assert plan[key] > 0
    assert plan["gain_at_t2"] > plan["gain_at_t1"]


# ── build_plans_for_rows: 策略结果行 → 计划 ─────────────
def test_build_plans_for_rows_basic():
    rows = [
        {"symbol": "600519.SH", "close": 100.0, "atr_14": 2.0, "low": 97.0, "name": "贵州茅台"},
        {"symbol": "000001.SZ", "close": 10.0, "atr_14": 0.2, "low": 9.8, "name": "平安银行"},
    ]
    plans = trade_plan.build_plans_for_rows(rows, equity=1_000_000)
    assert len(plans) == 2
    assert plans[0]["symbol"] == "600519.SH" and plans[0]["name"] == "贵州茅台"
    assert plans[0]["atr_estimated"] is False


def test_build_plans_for_rows_tolerates_missing_atr_column():
    """zdk 策略没有 atr_14 列 → 3% 兜底, 不抛异常。"""
    rows = [{"symbol": "600519.SH", "close": 100.0, "low": None, "name": ""}]
    plans = trade_plan.build_plans_for_rows(rows, equity=1_000_000)
    assert len(plans) == 1
    assert plans[0]["atr_estimated"] is True
    assert plans[0]["stop"] == pytest.approx(94.0)


def test_build_plans_for_rows_dedup_and_skip_invalid():
    rows = [
        {"symbol": "600519.SH", "close": 100.0},
        {"symbol": "600519.SH", "close": 101.0},   # 同标的重复 → 只留第一条
        {"symbol": "", "close": 10.0},             # 空 symbol → 跳过
        {"symbol": "000001.SZ"},                   # 无价格 → 跳过
        "not-a-dict",                              # 非法行 → 跳过
    ]
    plans = trade_plan.build_plans_for_rows(rows, equity=1_000_000)
    assert [p["symbol"] for p in plans] == ["600519.SH"]
    assert plans[0]["entry_ref"] == 100.0


def test_build_plans_for_rows_custom_keys():
    rows = [{"sym": "600519.SH", "px": 20.0, "atr": 0.4, "nm": "测试"}]
    plans = trade_plan.build_plans_for_rows(
        rows, equity=1_000_000,
        price_key="px", atr_key="atr", symbol_key="sym", name_key="nm",
    )
    assert len(plans) == 1 and plans[0]["name"] == "测试"


def test_build_plans_for_rows_accepts_polars_dicts_style_values():
    """策略行经过 _sanitize 后可能是 numpy/pl 标量, float 转换不能炸。"""
    df = pl.DataFrame({"symbol": ["600519.SH"], "close": [100.0], "atr_14": [2.0], "low": [97.0]})
    plans = trade_plan.build_plans_for_rows(df.to_dicts(), equity=1_000_000)
    assert len(plans) == 1 and plans[0]["stop"] == pytest.approx(97.0)


# ── latest_state: 大盘情绪读取 (优雅降级) ───────────────
def _write_regime(tmp_path, rows: list[dict]) -> None:
    d = tmp_path / "regime_history"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "date": [date.fromisoformat(r["date"]) for r in rows],
            "state": [r["state"] for r in rows],
            "score": [float(r["score"]) for r in rows],
        },
        schema={"date": pl.Date, "state": pl.String, "score": pl.Float64},
    ).write_parquet(d / "part.parquet")


def test_latest_state_reads_newest_row(tmp_path):
    _write_regime(tmp_path, [
        {"date": "2026-10-30", "state": "range", "score": 50.0},
        {"date": "2026-11-02", "state": "weak", "score": 12.5},
    ])
    state = trade_plan.latest_state(tmp_path)
    assert state is not None
    assert state["state"] == "weak"
    assert state["label"] == "弱势"
    assert state["score"] == pytest.approx(12.5)
    assert state["date"] == "2026-11-02"


def test_latest_state_missing_table_returns_none(tmp_path):
    """用户数据目录没有 regime_history 时必须优雅降级, 不能抛异常。"""
    assert trade_plan.latest_state(tmp_path) is None


def test_latest_state_ignores_unknown_state(tmp_path):
    _write_regime(tmp_path, [{"date": "2026-11-02", "state": "??", "score": 1.0}])
    assert trade_plan.latest_state(tmp_path) is None


# ── 买入执行方式 entry_mode ─────────────────────────────
def test_normalize_entry_mode():
    assert trade_plan.ENTRY_MODES == ("zone", "next_open", "market")
    assert trade_plan.DEFAULT_ENTRY_MODE == "zone"
    # 合法值原样 (含大小写/空白归一) 返回
    for mode in trade_plan.ENTRY_MODES:
        assert trade_plan.normalize_entry_mode(mode) == mode
        assert trade_plan.normalize_entry_mode(f"  {mode.upper()}  ") == mode
    # 缺失 / 非法 → 默认 zone (老配置与老计划靠这条兜底)
    assert trade_plan.normalize_entry_mode(None) == "zone"
    assert trade_plan.normalize_entry_mode("bogus") == "zone"
    assert trade_plan.normalize_entry_mode("") == "zone"
    assert trade_plan.normalize_entry_mode(3) == "zone"
    # default=None: 调用方要自己判非法 (API 用来区分「没传」与「传错」)
    assert trade_plan.normalize_entry_mode("bogus", default=None) is None
    assert trade_plan.normalize_entry_mode(None, default=None) is None
    assert trade_plan.normalize_entry_mode("market", default=None) == "market"


def test_merge_cfg_default_entry_mode():
    assert trade_plan.merge_cfg(None)["default_entry_mode"] == "zone"
    assert trade_plan.merge_cfg({"default_entry_mode": "next_open"})["default_entry_mode"] == "next_open"
    assert trade_plan.merge_cfg({"default_entry_mode": "MARKET"})["default_entry_mode"] == "market"
    # 非法值静默回落默认 (脏配置不能炸掉计划生成)
    assert trade_plan.merge_cfg({"default_entry_mode": "same_day"})["default_entry_mode"] == "zone"
    assert trade_plan.merge_cfg({"default_entry_mode": 1})["default_entry_mode"] == "zone"


def test_entry_mode_labels_cover_all_modes():
    assert set(trade_plan.ENTRY_MODE_LABELS) == set(trade_plan.ENTRY_MODES)
    assert all(trade_plan.ENTRY_MODE_LABELS[m] for m in trade_plan.ENTRY_MODES)


def test_build_plan_carries_entry_mode():
    """entry_mode 只决定「什么时候买」, 不影响止损/止盈/仓位口径。"""
    base = trade_plan.build_plan(symbol="600519.SH", price=100.0, equity=1_000_000, atr=2.0)
    assert base["entry_mode"] == "zone"          # 缺省 = cfg 默认
    for mode in trade_plan.ENTRY_MODES:
        plan = trade_plan.build_plan(
            symbol="600519.SH", price=100.0, equity=1_000_000, atr=2.0, entry_mode=mode
        )
        assert plan["entry_mode"] == mode
        # 价格与仓位口径完全一致, 只有 entry_mode 不同
        for key in ("entry_low", "entry_high", "stop", "stop_pct", "pos_pct",
                    "qty_planned", "target1", "target2", "regime_label", "timing_scale"):
            assert plan[key] == base[key], key
    # cfg 默认值生效; 显式入参优先于 cfg
    cfg = {"default_entry_mode": "market"}
    assert trade_plan.build_plan(
        symbol="600519.SH", price=100.0, equity=1_000_000, cfg=cfg
    )["entry_mode"] == "market"
    assert trade_plan.build_plan(
        symbol="600519.SH", price=100.0, equity=1_000_000, cfg=cfg, entry_mode="zone"
    )["entry_mode"] == "zone"
    # 非法显式值回落 cfg 默认 (纯函数层宽容, API 层才 400)
    assert trade_plan.build_plan(
        symbol="600519.SH", price=100.0, equity=1_000_000, entry_mode="bogus"
    )["entry_mode"] == "zone"


def test_build_plans_for_rows_passes_entry_mode():
    rows = [{"symbol": "600519.SH", "close": 100.0, "atr_14": 2.0}]
    assert trade_plan.build_plans_for_rows(rows, equity=1_000_000)[0]["entry_mode"] == "zone"
    assert trade_plan.build_plans_for_rows(
        rows, equity=1_000_000, entry_mode="next_open"
    )[0]["entry_mode"] == "next_open"
    # cfg 默认值透传
    assert trade_plan.build_plans_for_rows(
        rows, equity=1_000_000, cfg={"default_entry_mode": "market"}
    )[0]["entry_mode"] == "market"


# ── 优中选优: 排序 / 只数上限缩放 / 组合仓位分配 ────────
def test_merge_cfg_holdings_defaults_and_relative_bounds():
    base = trade_plan.merge_cfg(None)
    assert base["max_symbols"] == 0            # 0 = 不限制
    assert base["min_holdings"] == 1
    assert base["top_n"] == 10
    assert base["max_total_exposure_pct"] == 100.0
    # min_holdings 不能超过 max_symbols
    cfg = trade_plan.merge_cfg({"max_symbols": 3, "min_holdings": 10})
    assert cfg["min_holdings"] == 3 and cfg["max_symbols"] == 3
    # max_symbols=0 (不限制) 时保留下限原值
    assert trade_plan.merge_cfg({"max_symbols": 0, "min_holdings": 4})["min_holdings"] == 4
    # 越界回落默认
    assert trade_plan.merge_cfg({"min_holdings": 0})["min_holdings"] == 1
    assert trade_plan.merge_cfg({"top_n": 0})["top_n"] == 10


def test_rank_rows_sorts_by_score_desc():
    rows = [
        {"symbol": "A", "score": 30.0},
        {"symbol": "B", "score": 90.0},
        {"symbol": "C", "score": 60.0},
    ]
    ranked = trade_plan.rank_rows(rows, rank_key="score")
    assert [r["symbol"] for r in ranked] == ["B", "C", "A"]
    assert [r["symbol"] for r in trade_plan.rank_rows(rows, rank_key="score", rank_desc=False)] == [
        "A", "C", "B"
    ]
    # 自定义列名
    assert [r["symbol"] for r in trade_plan.rank_rows(rows, rank_key="symbol", rank_desc=False)] == [
        "A", "B", "C"
    ]


def test_rank_rows_keeps_order_when_column_missing():
    """没有 score 列 (或值不可比) 时保持原顺序 — 不破坏既有调用方。"""
    rows = [{"symbol": "A", "close": 10.0}, {"symbol": "B", "close": 20.0}]
    assert trade_plan.rank_rows(rows) == rows
    mixed = [{"symbol": "A", "score": 10.0}, {"symbol": "B"}]
    assert trade_plan.rank_rows(mixed) == mixed
    bad = [{"symbol": "A", "score": "x"}, {"symbol": "B", "score": 5.0}]
    assert trade_plan.rank_rows(bad) == bad
    assert trade_plan.rank_rows([], rank_key="score") == []


def test_effective_holdings_limit_scales_by_timing_and_phase():
    cfg = {"max_symbols": 5, "min_holdings": 1}
    # 无择时 + 无情绪 → 满额
    base = trade_plan.effective_holdings_limit(cfg, None)
    assert base["limit"] == 5 and base["unlimited"] is False and base["scaled"] is False
    assert base["timing_exposure"] == 1.0 and base["phase_exposure"] == 1.0
    # 大盘 risk_off(0.35) × 情绪 weak(0.3) → round(5*0.35*0.3)=round(0.525)=1
    tight = trade_plan.effective_holdings_limit(cfg, "weak", timing_exposure=0.35)
    assert tight["limit"] == 1
    assert tight["scaled"] is True
    assert "只数上限 5 → 本期最多 1 只" in tight["reason"]
    # 大盘 weak(0.65) × 情绪 range(0.75) → round(5*0.65*0.75)=round(2.4375)=2
    mid = trade_plan.effective_holdings_limit(cfg, "range", timing_exposure=0.65)
    assert mid["limit"] == 2
    # 缩到 0 也被 min_holdings 兜住
    floor = trade_plan.effective_holdings_limit(
        {"max_symbols": 5, "min_holdings": 2}, "weak", timing_exposure=0.35
    )
    assert floor["limit"] == 2


def test_effective_holdings_limit_zero_means_unlimited():
    out = trade_plan.effective_holdings_limit({"max_symbols": 0}, "weak", timing_exposure=0.35)
    assert out["limit"] is None and out["unlimited"] is True
    assert "不限制" in out["reason"]


def test_trend_exposure_degrades_without_data(tmp_path):
    """没有行情数据时必须优雅降级为 unknown(0.5), 不抛异常。"""
    out = trade_plan.trend_exposure(tmp_path)
    assert set(out) == {"state", "label", "exposure", "ok"}
    assert out["ok"] is False
    assert out["exposure"] > 0


def test_allocate_portfolio_scales_down_and_rounds_lots():
    plans = [
        {"symbol": "A", "pos_pct": 20.0, "entry_ref": 10.0, "stop": 9.0},
        {"symbol": "B", "pos_pct": 20.0, "entry_ref": 20.0, "stop": 18.0},
        {"symbol": "C", "pos_pct": 20.0, "entry_ref": 40.0, "stop": 36.0},
    ]
    kept, dropped, summary = trade_plan.allocate_portfolio(
        plans, equity=1_000_000, cfg={"max_total_exposure_pct": 30.0}
    )
    assert dropped == []
    assert summary["scaled"] is True and summary["scale"] == pytest.approx(0.5)
    assert summary["requested_pct"] == pytest.approx(60.0)
    assert summary["total_pos_pct"] == pytest.approx(30.0)
    assert [p["pos_pct"] for p in kept] == [10.0, 10.0, 10.0]
    assert all(p["pos_pct_scaled"] is True for p in kept)
    assert all(p["portfolio_pos_pct"] == p["pos_pct"] for p in kept)
    # 整手重算: 1e6*10%/10 = 10000 股; 1e6*10%/20=5000; 1e6*10%/40=2500
    assert [p["qty_planned"] for p in kept] == [10000, 5000, 2500]
    assert all(p["qty_planned"] % 100 == 0 for p in kept)
    # 风险金额跟着股数重算
    assert kept[0]["risk_amount"] == pytest.approx(1.0 * 10000)


def test_allocate_portfolio_drops_when_scaled_below_one_lot():
    plans = [
        {"symbol": "A", "pos_pct": 20.0, "entry_ref": 10.0, "stop": 9.0},
        {"symbol": "EXPENSIVE", "pos_pct": 20.0, "entry_ref": 500_000.0, "stop": 450_000.0},
    ]
    kept, dropped, summary = trade_plan.allocate_portfolio(
        plans, equity=1_000_000, cfg={"max_total_exposure_pct": 10.0}
    )
    assert summary["scale"] == pytest.approx(0.25)
    assert [p["symbol"] for p in kept] == ["A"]
    # 20% × 0.25 = 5% → 1e6*5%/10 = 5000 股
    assert kept[0]["qty_planned"] == 5000
    assert len(dropped) == 1
    assert dropped[0]["symbol"] == "EXPENSIVE"
    assert dropped[0]["reason"] == "组合仓位上限压缩后不足一手, 未生成计划"


def test_allocate_portfolio_no_scale_when_under_cap():
    plans = [{"symbol": "A", "pos_pct": 10.0, "entry_ref": 10.0, "stop": 9.0}]
    kept, dropped, summary = trade_plan.allocate_portfolio(plans, equity=1_000_000)
    assert dropped == [] and summary["scaled"] is False and summary["scale"] == 1.0
    assert kept[0]["pos_pct"] == 10.0 and kept[0]["pos_pct_scaled"] is False
    assert kept[0]["qty_planned"] == 10000


def test_allocate_portfolio_empty_input():
    kept, dropped, summary = trade_plan.allocate_portfolio([], equity=1_000_000)
    assert kept == [] and dropped == []
    assert summary["total_pos_pct"] == 0.0 and summary["scaled"] is False
