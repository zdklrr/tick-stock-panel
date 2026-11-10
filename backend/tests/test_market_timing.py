"""大盘择时 (market_timing) 纯函数与合成口径测试。

覆盖: MA 计算 / 四态四分支 / 本地日K 缺失降级 unknown / 三路相乘与夹取边界 /
极弱态文案 / parts.missing 标记 / 不联网。

设计: 日K 一律写进 tmp_path 的 parquet 分区 (与线上存储同结构), 用 monkeypatch 把
`settings.data_dir` 指向 tmp_path —— 这样测的是真实读盘路径, 不 mock 掉扫描逻辑。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services import market_timing as mt


# ── fixture: 造本地日K ──────────────────────────────────
def _write_klines(
    data_dir,
    table: str,
    symbol: str,
    closes: list[float],
    *,
    start: date = date(2026, 5, 1),
) -> None:
    """按线上分区结构写 parquet: <data_dir>/<table>/date=YYYY-MM-DD/part.parquet。"""
    for i, close in enumerate(closes):
        day = start + timedelta(days=i)
        part = data_dir / table / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": [symbol],
            "date": [day],
            "close": [float(close)],
        }).write_parquet(part / "part.parquet")


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """把 settings.data_dir 钉到 tmp_path, 避免任何测试读到真实用户数据目录。"""
    from app import config

    monkeypatch.setattr(config.settings, "data_dir", tmp_path, raising=False)
    return tmp_path


def _ramp(n: int, start: float, step: float) -> list[float]:
    return [start + step * i for i in range(n)]


# ── MA 计算 ────────────────────────────────────────────
def test_ma_basic_and_rounding():
    closes = [float(i) for i in range(1, 11)]  # 1..10
    # 最近 3 根 = 8,9,10 → 9.0
    assert mt.ma(closes, 3) == 9.0
    # 最近 5 根 = 6..10 → 8.0
    assert mt.ma(closes, 5) == 8.0


def test_ma_insufficient_bars_returns_none():
    assert mt.ma([1.0, 2.0], 5) is None
    assert mt.ma([], 20) is None
    assert mt.ma([1.0, 2.0, 3.0], 0) is None


def test_ma_rejects_non_finite():
    assert mt.ma([1.0, float("nan"), 3.0], 2) is None
    assert mt.ma([1.0, float("inf")], 2) is None


# ── 四态四分支 ─────────────────────────────────────────
def test_classify_above_both_is_risk_on():
    assert mt.classify_trend(close=110.0, ma20=105.0, ma60=100.0) == mt.TREND_RISK_ON


def test_classify_above_ma20_only_is_weak():
    """只站上 MA20 (跌破 MA60) → weak, advice 是「轻仓试单」。"""
    state = mt.classify_trend(close=105.0, ma20=100.0, ma60=110.0)
    assert state == mt.TREND_WEAK
    advice = mt.trend_advice(state, close=105.0, ma20=100.0, ma60=110.0)
    assert "轻仓试单" in advice


def test_classify_above_ma60_only_is_weak_with_half_position_advice():
    """只站上 MA60 (跌破 MA20) → 同为 weak, 但 advice 是「半仓」。"""
    state = mt.classify_trend(close=105.0, ma20=110.0, ma60=100.0)
    assert state == mt.TREND_WEAK
    advice = mt.trend_advice(state, close=105.0, ma20=110.0, ma60=100.0)
    assert "半仓" in advice


def test_classify_below_both_is_risk_off_with_empty_position_advice():
    state = mt.classify_trend(close=95.0, ma20=100.0, ma60=105.0)
    assert state == mt.TREND_RISK_OFF
    advice = mt.trend_advice(state, close=95.0, ma20=100.0, ma60=105.0)
    assert "空仓" in advice and "仅留底仓" in advice
    # 四态系数: risk_off 最低但**不为 0** (不做二元闸门)
    assert mt.TREND_EXPOSURE[mt.TREND_RISK_OFF] == 0.35
    assert all(v > 0 for v in mt.TREND_EXPOSURE.values())


def test_classify_unknown_when_closes_missing():
    assert mt.classify_trend(None, 100.0, 100.0) == mt.TREND_UNKNOWN
    assert mt.classify_trend(100.0, None, None) == mt.TREND_UNKNOWN


def test_classify_single_ma_degrades_conservatively():
    """只有一条均线时: 能确认的跌破优先给 risk_off, 站上但缺另一条只能给 weak。"""
    assert mt.classify_trend(95.0, None, 100.0) == mt.TREND_RISK_OFF
    assert mt.classify_trend(105.0, None, 100.0) == mt.TREND_WEAK
    assert mt.classify_trend(95.0, 100.0, None) == mt.TREND_RISK_OFF
    assert mt.classify_trend(105.0, 100.0, None) == mt.TREND_WEAK


# ── 读盘: 真实 parquet ─────────────────────────────────
def test_trend_timing_risk_on_from_local_index_klines(tmp_path):
    """本地有 000300.SH 且一路上行 → risk_on, 各字段齐全。"""
    closes = _ramp(120, 100.0, 1.0)  # 100 → 219, 单调上涨
    _write_klines(tmp_path, "kline_index_daily", "000300.SH", closes)

    snap = mt.trend_timing(tmp_path)
    assert snap["ok"] is True
    assert snap["state"] == mt.TREND_RISK_ON
    assert snap["state_label"] == "趋势偏多"
    assert snap["exposure"] == 1.0
    assert snap["symbol"] == "000300.SH"
    assert snap["bars"] == 120
    assert snap["close"] == pytest.approx(219.0, abs=1e-6)
    # MA20 是最近 20 根均值 = 200..219 → 209.5
    assert snap["ma20"] == pytest.approx(209.5, abs=1e-6)
    # MA60 是最近 60 根均值 = 160..219 → 189.5
    assert snap["ma60"] == pytest.approx(189.5, abs=1e-6)
    # 偏离是**百分数数值** (不是小数)
    assert snap["pct_vs_ma20"] == pytest.approx((219 - 209.5) / 209.5 * 100, abs=0.01)
    assert snap["as_of"] is not None


def test_trend_timing_risk_off_from_local_klines(tmp_path):
    closes = _ramp(120, 220.0, -1.0)  # 下跌
    _write_klines(tmp_path, "kline_index_daily", "000300.SH", closes)
    snap = mt.trend_timing(tmp_path)
    assert snap["ok"] is True
    assert snap["state"] == mt.TREND_RISK_OFF
    assert snap["state_label"] == "空头趋势"
    assert snap["exposure"] == 0.35
    assert snap["pct_vs_ma20"] < 0


def test_trend_timing_prefers_etf_when_present(tmp_path):
    """ETF 表有 510300 时优先用它 (zdk 原本的口径标的就是 ETF)。"""
    _write_klines(tmp_path, "kline_etf_daily", "510300", _ramp(120, 3.0, 0.01))
    _write_klines(tmp_path, "kline_index_daily", "000300.SH", _ramp(120, 100.0, 1.0))
    snap = mt.trend_timing(tmp_path)
    assert snap["symbol"] == "510300"
    assert snap["close"] < 10  # 用的是 ETF 价格, 不是指数点位


def test_trend_timing_missing_local_data_degrades_to_unknown(tmp_path):
    """本地完全没日K → ok=False/unknown, 系数 0.5, 不抛异常、不联网。"""
    snap = mt.trend_timing(tmp_path)
    assert snap["ok"] is False
    assert snap["state"] == mt.TREND_UNKNOWN
    assert snap["state_label"] == "数据缺失"
    assert snap["exposure"] == 0.5
    assert snap["bars"] == 0
    assert snap["close"] is None
    assert "缺少" in snap["advice"]


def test_trend_timing_insufficient_bars_degrades_to_unknown(tmp_path):
    """只有 30 根 (< 60) 算不出 MA60 → unknown。"""
    _write_klines(tmp_path, "kline_index_daily", "000300.SH", _ramp(30, 100.0, 1.0))
    snap = mt.trend_timing(tmp_path)
    assert snap["ok"] is False
    assert snap["state"] == mt.TREND_UNKNOWN
    assert snap["bars"] == 30


def test_trend_timing_corrupt_parquet_degrades(tmp_path):
    """parquet 损坏 → 降级 unknown 而不是抛。"""
    part = tmp_path / "kline_index_daily" / "date=2026-11-02"
    part.mkdir(parents=True)
    (part / "part.parquet").write_bytes(b"not a parquet file")
    snap = mt.trend_timing(tmp_path)
    assert snap["state"] == mt.TREND_UNKNOWN
    assert snap["ok"] is False


# ── 合成: 三路相乘与夹取 ───────────────────────────────
def _patch_parts(monkeypatch, *, timing=1.0, phase=1.0, risk=1.0,
                 timing_missing=False, phase_missing=False, risk_missing=False):
    """把三路取数替换成固定系数 (只测合成数学, 不碰网络/磁盘)。"""
    monkeypatch.setattr(
        mt, "trend_timing",
        lambda *a, **kw: {
            "ok": not timing_missing,
            "symbol": "000300.SH", "as_of": "2026-11-02",
            "close": 100.0, "ma20": 101.0, "ma60": 102.0,
            "pct_vs_ma20": -0.99, "pct_vs_ma60": -1.96,
            "state": mt.TREND_UNKNOWN if timing_missing else mt.TREND_WEAK,
            "state_label": "数据缺失" if timing_missing else "趋势偏弱",
            "exposure": 1.0 if timing_missing else timing,
            "bars": 0 if timing_missing else 120,
            "advice": "x",
        },
    )
    monkeypatch.setattr(
        mt, "_phase_part",
        lambda d: {
            "key": "phase", "label": "情绪周期",
            "value": 1.0 if phase_missing else phase,
            "missing": phase_missing,
            "state": None if phase_missing else "range",
            "state_label": None if phase_missing else "震荡",
            "detail": "数据缺失 (按 1.0 计)" if phase_missing else "震荡 (2026-11-02)",
        },
    )
    monkeypatch.setattr(
        mt, "_risk_part",
        lambda d, a: {
            "key": "risk", "label": "外围风险",
            "value": 1.0 if risk_missing else risk,
            "missing": risk_missing,
            "state": None if risk_missing else "elevated",
            "state_label": None if risk_missing else "偏高",
            "detail": "数据缺失 (按 1.0 计)" if risk_missing else "风险分 69.9 (偏高)",
        },
    )
    monkeypatch.setattr(mt, "_paper_part", lambda d, a: {
        "active_plans": 0, "last_adj": None, "last_adj_reason": None,
        "timing_scale": None, "note": "该建议将在你新建交易计划时生效。",
    })


def test_synthesize_multiplies_three_parts(monkeypatch):
    _patch_parts(monkeypatch, timing=0.65, phase=0.75, risk=0.93)
    out = mt.synthesize(Path("."))
    assert out["ok"] is True
    # 0.65 × 0.75 × 0.93 = 0.453375 → 0.453
    assert out["suggested_exposure"] == pytest.approx(0.453, abs=1e-6)
    assert out["suggested_exposure_pct"] == pytest.approx(45.3, abs=0.05)
    assert out["label"] == "半仓为宜"
    assert set(out["parts"]) == {"timing", "phase", "risk"}
    assert out["detail"]["clamped"] is False


def test_synthesize_clamps_to_floor_and_never_zero(monkeypatch):
    """三路都极弱 → 夹到 0.15 保底, 绝不归零 (二元闸门会「一天 0 成交」)。"""
    _patch_parts(monkeypatch, timing=0.35, phase=0.3, risk=0.35)
    out = mt.synthesize(Path("."))
    # 0.35 × 0.3 × 0.35 = 0.03675 → 夹到 0.15
    assert out["suggested_exposure"] == pytest.approx(mt.SYN_FLOOR, abs=1e-9)
    assert out["suggested_exposure"] > 0
    assert out["detail"]["clamped"] is True
    assert out["label"] == "建议空仓或仅留底仓"
    assert "空仓" in out["advice"] or "仅留底仓" in out["advice"]


def test_synthesize_clamps_to_cap(monkeypatch):
    _patch_parts(monkeypatch, timing=1.0, phase=1.2, risk=1.1)
    out = mt.synthesize(Path("."))
    # 1.0 × 1.2 × 1.1 = 1.32 → 夹到 1.20
    assert out["suggested_exposure"] == pytest.approx(mt.SYN_CAP, abs=1e-9)
    assert out["detail"]["clamped"] is True
    assert out["label"] == "可以满仓做多"


def test_synthesize_missing_part_contributes_neutral_and_is_flagged(monkeypatch):
    """缺失的一路按 1.0 参与且标 missing=True, 不静默当 0。"""
    _patch_parts(monkeypatch, timing=0.65, phase=0.75, risk=1.0,
                 risk_missing=True)
    out = mt.synthesize(Path("."))
    assert out["parts"]["risk"]["missing"] is True
    assert out["parts"]["risk"]["value"] == 1.0
    # 0.65 × 0.75 × 1.0 = 0.4875 → round(,3) = 0.488
    assert out["suggested_exposure"] == pytest.approx(0.488, abs=1e-9)
    assert "外围风险" in out["advice"] and "缺失" in out["advice"]


def test_synthesize_all_missing_still_returns_neutral(monkeypatch):
    _patch_parts(monkeypatch, timing_missing=True, phase_missing=True, risk_missing=True)
    out = mt.synthesize(Path("."))
    assert out["suggested_exposure"] == pytest.approx(1.0, abs=1e-6)
    assert out["label"] == "可以满仓做多"
    assert "缺失" in out["advice"]
    assert all(p["missing"] for p in out["parts"].values())


def test_synthesize_names_the_worst_part(monkeypatch):
    """advice 要写出「谁在压仓位」—— 最弱那一路必须被点名。"""
    _patch_parts(monkeypatch, timing=0.35, phase=0.75, risk=0.93)
    out = mt.synthesize(Path("."))
    assert "大盘择时" in out["advice"]
    assert "×0.35" in out["advice"]


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (1.20, "可以满仓做多"),
        (1.00, "可以满仓做多"),
        (0.99, "正常仓位"),
        (0.70, "正常仓位"),
        (0.69, "半仓为宜"),
        (0.45, "半仓为宜"),
        (0.44, "轻仓试单"),
        (0.25, "轻仓试单"),
        (0.24, "建议空仓或仅留底仓"),
        (0.15, "建议空仓或仅留底仓"),
    ],
)
def test_synth_label_tiers(value, label):
    assert mt.synth_label(value) == label


def test_trend_exposure_table_matches_zdk():
    assert mt.TREND_EXPOSURE == {"risk_on": 1.0, "weak": 0.65, "risk_off": 0.35, "unknown": 0.5}
    assert mt.TREND_LABELS == {
        "risk_on": "趋势偏多", "weak": "趋势偏弱",
        "risk_off": "空头趋势", "unknown": "数据缺失",
    }


# ── 自愈门控 (v0.3.39): 生产路径必须放行, 测试/临时目录必须挡住 ────
def test_healable_dir_allows_production_data_dir(tmp_path, monkeypatch):
    """HTTP 层显式传 settings.data_dir 时必须允许自愈。

    这是 v0.3.39 修的真 bug: 原门控按「调用方有没有省掉 data_dir」判断, 而
    api/risk.py 一律显式传 `repo.store.data_dir` → 生产路径上自愈是死代码,
    桌面端大盘择时长期停在旧日期。这里用一个**不在系统临时目录**的路径验证。
    """
    import shutil
    import tempfile

    from app import config

    tmp_root = Path(tempfile.gettempdir()).resolve()
    prod = tmp_root.parent / "_tsp_heal_probe" / "data"
    try:
        prod.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("无法创建临时目录外的探测目录")
    try:
        monkeypatch.setattr(config.settings, "data_dir", prod, raising=False)
        assert mt._is_healable_dir(None) is True       # None → 走 settings, 放行
        assert mt._is_healable_dir(prod) is True       # 显式传同一路径, 放行
        assert mt._is_healable_dir(prod / "sub") is False  # 别的路径 → 挡住
    finally:
        shutil.rmtree(prod.parent, ignore_errors=True)


def test_healable_dir_blocks_other_dirs_and_tempdir(tmp_path, monkeypatch):
    """显式传别的目录 / 系统临时目录一律只读不自愈 (避免单测联网写盘)。"""
    from app import config

    other = tmp_path / "other_dir"
    other.mkdir()
    monkeypatch.setattr(config.settings, "data_dir", tmp_path, raising=False)

    assert mt._is_healable_dir(other) is False     # 与 settings 不同路径
    assert mt._is_healable_dir(tmp_path) is False  # 就是系统临时目录下的路径


def test_trend_timing_does_not_heal_on_tmp_path(tmp_path, monkeypatch):
    """tmp_path 数据 (120 根单调上涨) 必须原样返回 risk_on, 不被联网回补污染。"""
    from app import config

    closes = _ramp(120, 100.0, 1.0)
    _write_klines(tmp_path, "kline_index_daily", "000300.SH", closes)
    monkeypatch.setattr(config.settings, "data_dir", tmp_path, raising=False)

    snap = mt.trend_timing(tmp_path)
    assert snap["state"] == mt.TREND_RISK_ON
    assert snap["close"] == pytest.approx(219.0, abs=1e-6)
    assert snap.get("backfilled") is None


# ── 交易日口径的滞后判定 (v0.3.40) ──────────────────────
# 背景: 原自愈触发条件是「自然日 > 1」。交易日 15:00 收盘后, as_of 仍停在昨日
# 时自然日差恒为 1, 永远达不到 > 1 → **整个晚上都不回补**, 卡片挂「滞后 1 天」
# 直到次日。用户报「本地的大盘择时不是今天的数据」就是这个。
_TZ = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))


def _cn(*args) -> object:
    from datetime import datetime

    return datetime(*args, tzinfo=_TZ)


@pytest.mark.parametrize(
    ("label", "now", "as_of", "expected", "behind"),
    [
        # 交易日 09:30 开盘后: 当日已有一根未定型盘中K → 昨日 as_of 就是落后
        # (v0.3.41: v0.3.40 只认收盘后, 导致整个交易时段都不回补)
        ("11-05 09:30 开盘", _cn(2026, 11, 5, 9, 30), "2026-11-04", date(2026, 11, 5), True),
        ("11-05 10:24 盘中", _cn(2026, 11, 5, 10, 24), "2026-11-04", date(2026, 11, 5), True),
        ("11-05 14:00 盘中", _cn(2026, 11, 5, 14, 0), "2026-11-04", date(2026, 11, 5), True),
        # 开盘前: 当日还没有K, 昨日 as_of 即最新
        ("11-05 00:12 凌晨", _cn(2026, 11, 5, 0, 12), "2026-11-04", date(2026, 11, 4), False),
        ("11-05 09:24 竞价中", _cn(2026, 11, 5, 9, 24), "2026-11-04", date(2026, 11, 4), False),
        # 09:25 竞价结束 → 当日已有开盘价, 昨日 as_of 就是落后 (用户明确的「最迟」)
        ("11-05 09:25 竞价结束", _cn(2026, 11, 5, 9, 25), "2026-11-04", date(2026, 11, 5), True),
        ("11-05 09:29 盘前", _cn(2026, 11, 5, 9, 29), "2026-11-04", date(2026, 11, 5), True),
        ("11-05 15:04 收盘前", _cn(2026, 11, 5, 15, 4), "2026-11-04", date(2026, 11, 5), True),
        # 收盘后 15:05 起: 当日日K 定型 → 昨日 as_of 仍是落后
        ("11-05 15:35 收盘后", _cn(2026, 11, 5, 15, 35), "2026-11-04", date(2026, 11, 5), True),
        ("11-06 10:00 次日盘中", _cn(2026, 11, 6, 10, 0), "2026-11-04", date(2026, 11, 6), True),
        # 周末: 应有数据是上周五, 周五 as_of 即最新 (不再虚报滞后)
        ("11-07 周六 10:00", _cn(2026, 11, 7, 10, 0), "2026-11-06", date(2026, 11, 6), False),
        # 周一盘前: 应有数据仍是上周五
        ("11-09 周一 09:00", _cn(2026, 11, 9, 9, 0), "2026-11-06", date(2026, 11, 6), False),
        # 周一开盘后: 应有数据是周一
        ("11-09 周一 10:00", _cn(2026, 11, 9, 10, 0), "2026-11-06", date(2026, 11, 9), True),
    ],
)
def test_expected_as_of_uses_trading_days(label, now, as_of, expected, behind):
    assert mt._expected_as_of(now) == expected, label
    assert mt._is_behind_expected(as_of, now) is behind, label


def test_stale_days_counts_trading_days_not_natural_days():
    """滞后天数是「缺了几个交易日」, 不是自然日差 —— 周末不虚报。"""
    # 周二 15:35 看周五的数据 → 缺周一、周二 2 个交易日 (自然日差是 4)
    assert mt._stale_days("2026-11-06", _cn(2026, 11, 10, 15, 35)) == 2
    # 周五 15:35 看周四 → 1 个交易日
    assert mt._stale_days("2026-11-05", _cn(2026, 11, 6, 15, 35)) == 1
    # 周六看周五 → 0 (原自然日口径会算成 1, 卡片白白显示「滞后 1 天」)
    assert mt._stale_days("2026-11-06", _cn(2026, 11, 7, 10, 0)) == 0
    # 收盘后看昨日 → 1 (当日日K 应已可得, 这才是要触发回补的那一格)
    assert mt._stale_days("2026-11-04", _cn(2026, 11, 5, 15, 35)) == 1
    # 盘中看昨日 → 1 (v0.3.41: 开盘后当日K 就已经该有了)
    assert mt._stale_days("2026-11-04", _cn(2026, 11, 5, 10, 24)) == 1
    # 开盘前看昨日 → 0 (昨日就是最新)
    assert mt._stale_days("2026-11-04", _cn(2026, 11, 5, 9, 0)) == 0
    # 未来日期不报负
    assert mt._stale_days("2026-11-09", _cn(2026, 11, 5, 15, 35)) == 0


def test_is_intraday_bar_marks_unsettled_candle():
    """盘中当日K 标 intraday=True (界面提示「未定型」), 收盘后/历史日 False。"""
    assert mt._is_intraday_bar("2026-11-05", _cn(2026, 11, 5, 10, 24)) is True
    assert mt._is_intraday_bar("2026-11-05", _cn(2026, 11, 5, 9, 25)) is True
    assert mt._is_intraday_bar("2026-11-05", _cn(2026, 11, 5, 9, 24)) is False
    assert mt._is_intraday_bar("2026-11-05", _cn(2026, 11, 5, 15, 5)) is False
    assert mt._is_intraday_bar("2026-11-04", _cn(2026, 11, 5, 10, 24)) is False
    assert mt._is_intraday_bar(None, _cn(2026, 11, 5, 10, 24)) is False
    assert mt._is_intraday_bar("garbage", _cn(2026, 11, 5, 10, 24)) is False
    assert mt._is_intraday_bar("2026-11-07", _cn(2026, 11, 7, 10, 0)) is False  # 周六



def test_heal_triggers_after_close_when_data_is_yesterday(monkeypatch, tmp_path):
    """收盘后 as_of 停在昨日必须触发回补 (v0.3.39 的旧条件在这里是死代码)。

    这里只验证触发条件本身, 不联网: 把 ensure_timing_index_fresh 打桩。
    """
    from app import config
    from app.services import index_daily_free as idf

    prod = tmp_path / "prod_data"
    prod.mkdir()
    monkeypatch.setattr(config.settings, "data_dir", prod, raising=False)
    # 绕过临时目录限制: 直接打桩 _is_healable_dir
    monkeypatch.setattr(mt, "_is_healable_dir", lambda _d: True)
    monkeypatch.setattr(mt, "_is_behind_expected", lambda as_of, now=None: True)
    calls = {"n": 0}

    def _fake_heal(data_dir=None, *, force=False):
        calls["n"] += 1
        return {"ok": True, "fetched": {}, "reason": "fetch-empty"}

    monkeypatch.setattr(idf, "ensure_timing_index_fresh", _fake_heal)
    mt.trend_timing(prod)
    assert calls["n"] == 1


def test_intraday_bar_does_not_block_request(monkeypatch, tmp_path):
    """盘中未定型时**必须立刻返回**, 后台去刷 (v0.3.41)。

    v0.3.41 第一版把盘中刷新也做成同步调用, 页面上四张卡同时请求 →
    NAS 上 `/api/risk/timing` 直接 180 秒超时。这里锁住「请求路径不等网络」。
    """
    import threading as _threading
    import time as _time

    from app import config
    from app.services import index_daily_free as idf

    prod = tmp_path / "prod_data"
    prod.mkdir()
    monkeypatch.setattr(config.settings, "data_dir", prod, raising=False)
    monkeypatch.setattr(mt, "_is_healable_dir", lambda _d: True)
    monkeypatch.setattr(mt, "_is_behind_expected", lambda as_of, now=None: False)
    monkeypatch.setattr(mt, "_is_intraday_bar", lambda as_of, now=None: True)
    # 数据是有的 (ok=True), 只是当天那根未定型 —— 这才是「只起后台线程」的分支
    monkeypatch.setattr(
        mt,
        "_build_snapshot",
        lambda data_dir, symbol, days: {
            "ok": True, "symbol": "510300", "as_of": "2026-11-05",
            "close": 4.19, "ma20": 4.20, "ma60": 4.48,
            "pct_vs_ma20": -0.2, "pct_vs_ma60": -6.5,
            "state": "risk_off", "state_label": "空头趋势", "exposure": 0.35,
            "bars": 120, "stale_days": 0, "stale": False,
            "intraday": True, "advice": "轻仓",
        },
    )
    # 重置节流状态, 否则前面用例刚 kick 过, 这里根本不会起线程
    monkeypatch.setattr(mt, "_intraday_kicked_at", 0.0, raising=False)
    monkeypatch.setattr(mt, "_intraday_thread", None, raising=False)

    sync_calls = {"n": 0}

    def _slow_heal(data_dir=None, *, force=False):
        # 只有请求线程 (MainThread) 走到这里才算「同步等网络」;
        # 后台线程自己调这个是设计如此, 不算阻塞。
        if _threading.current_thread().name == "MainThread":
            sync_calls["n"] += 1
        return {"ok": True, "fetched": {}, "reason": "fresh"}

    monkeypatch.setattr(idf, "ensure_timing_index_fresh", _slow_heal)

    t0 = _time.time()
    payload = mt.trend_timing(prod)
    elapsed = _time.time() - t0
    # 关键断言: 请求没有同步等网络
    assert sync_calls["n"] == 0
    assert elapsed < 1.0
    assert payload.get("intraday") is True
    # 等后台线程跑完 (它的桩立刻返回, 不会真等网络)
    thread = mt._intraday_thread
    if thread is not None:
        thread.join(3.0)


def test_kick_intraday_refresh_is_throttled(monkeypatch, tmp_path):
    """连续多次请求只放行一个后台刷新线程 (节流 + 存活检查)。"""
    import threading as _threading

    from app.services import index_daily_free as idf

    monkeypatch.setattr(mt, "_intraday_kicked_at", 0.0, raising=False)
    monkeypatch.setattr(mt, "_intraday_thread", None, raising=False)
    calls = {"n": 0}
    gate = _threading.Event()

    def _counting(data_dir=None, *, force=False):
        calls["n"] += 1
        gate.wait(2.0)
        return {"ok": True, "fetched": {}, "reason": "backfilled"}

    monkeypatch.setattr(idf, "ensure_timing_index_fresh", _counting)
    for _ in range(5):
        mt._kick_intraday_refresh(tmp_path)
    gate.set()
    assert calls["n"] <= 1

