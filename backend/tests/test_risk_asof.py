"""风险雷达 / 盘前简报的「数据日期」字段 (as_of) 单元测试: 全程不联网。

背景: 用户反馈看板四张卡里只有情绪周期显示日期, 盘前简报与风险雷达完全没有
时间信息, 用户分不清看到的是今天还是昨天的数据。于是两处返回值都补了 `as_of`:
- `radar.as_of`      = 所有**可用且未过期**指数里最新的数据日期;
- `premarket.as_of`  = 今天 (简报问的是「今天开门怎么开」), 而 `us/asia` 各自
  带自己区域的数据日期, `a50/cnh` 是实时报价所以是今天。

本文件锁定三件事:
1. `as_of` 的形状: 要么是合法的 `YYYY-MM-DD`, 要么是 None, 绝不混入时间戳;
2. 语义: `stale` 的行不参与整体日期计算 (不能拿旧数据冒充最新);
3. 降级: 任何取数失败都降级成 `None` 且**键一定存在** (前端不用判空两种形状)。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services import global_risk, premarket

# ---------------------------------------------------------------- 工具


def _row(
    code: str,
    *,
    region: str,
    as_of: str | None,
    stale: bool = False,
    price: float | None = 100.0,
    chg_pct: float | None = 1.0,
) -> dict:
    """构造一条 `_fetch_indexes` 形状的指数行。"""
    return {
        "code": code,
        "name": code,
        "region": region,
        "weight": 1.0,
        "price": price,
        "chg_pct": chg_pct,
        "ma20_pct": None,
        "score": None if stale else global_risk.index_score(chg_pct),
        "as_of": as_of,
        "source": "missing" if stale else "sina_gb",
        "stale": stale,
    }


def _assert_date_or_none(value, *, field: str = "as_of") -> None:
    """口径硬约定: 只能是 'YYYY-MM-DD' 字符串或 None。"""
    if value is None:
        return
    assert isinstance(value, str), f"{field} 必须是字符串或 None, 实际 {type(value)}"
    assert len(value) == 10, f"{field} 必须是 YYYY-MM-DD, 实际 {value!r}"
    # fromisoformat 能解析才是合法日期 (也顺带排除了 '2026-11-3' 这种短格式)
    date.fromisoformat(value)


# ---------------------------------------------------------------- radar: 正常路径


def test_radar_as_of_takes_latest_usable_row():
    """整体 as_of = 所有可用行里最新的那天。"""
    rows = [
        _row("IXIC", region="us", as_of="2026-11-02"),
        _row("SPX", region="us", as_of="2026-11-01"),
        _row("N225", region="asia", as_of="2026-11-03"),
    ]
    today = date(2026, 11, 3)
    as_of, stale_days = global_risk._overall_as_of(rows, today)
    assert as_of == "2026-11-03"
    assert stale_days == 0
    _assert_date_or_none(as_of)


def test_radar_as_of_ignores_stale_rows():
    """stale 的行不参与计算 —— 不能拿「被丢弃的旧数据」冒充最新日期。"""
    rows = [
        _row("IXIC", region="us", as_of="2026-11-02"),
        # 这条 stale 且日期更新, 但它已被丢弃, 不能把整体日期顶到 11-03
        _row("TWII", region="asia", as_of="2026-11-03", stale=True),
    ]
    as_of, stale_days = global_risk._overall_as_of(rows, date(2026, 11, 4))
    assert as_of == "2026-11-02"
    assert stale_days == 2


def test_radar_as_of_ignores_rows_without_price_or_chg():
    """没有价格/涨跌幅的行不参与 (即使 it 有 as_of)。"""
    rows = [
        _row("IXIC", region="us", as_of="2026-11-02"),
        _row("SPX", region="us", as_of="2026-11-03", price=None),
        _row("N225", region="asia", as_of="2026-11-03", chg_pct=None),
    ]
    as_of, _ = global_risk._overall_as_of(rows, date(2026, 11, 3))
    assert as_of == "2026-11-02"


def test_radar_as_of_none_when_no_usable_date():
    """一行可用的都没有 → (None, None), 不编日期。"""
    rows = [
        _row("IXIC", region="us", as_of=None),
        _row("TWII", region="asia", as_of="2026-11-03", stale=True),
    ]
    assert global_risk._overall_as_of(rows, date(2026, 11, 3)) == (None, None)
    assert global_risk._overall_as_of([], date(2026, 11, 3)) == (None, None)


def test_radar_as_of_tolerates_garbage_date_string():
    """脏日期串不能炸 (源偶尔给出奇怪格式)。"""
    rows = [
        _row("IXIC", region="us", as_of="not-a-date"),
        _row("SPX", region="us", as_of="2026-11-02"),
    ]
    as_of, _ = global_risk._overall_as_of(rows, date(2026, 11, 3))
    assert as_of == "2026-11-02"


def test_radar_build_payload_has_as_of_keys(tmp_path):
    """离线组装 (allow_fetch=False) 的 payload 也必须带 as_of / stale_days 键。"""
    payload = global_risk._build(tmp_path, "default", False)
    assert "as_of" in payload
    assert "stale_days" in payload
    assert payload["ok"] is False  # 离线时没有可用指数
    _assert_date_or_none(payload["as_of"])
    assert payload["as_of"] is None


# ---------------------------------------------------------------- radar: 降级路径


def test_radar_degraded_payload_keeps_as_of_keys(monkeypatch, tmp_path):
    """整体组装炸掉时, 降级 payload 仍有 as_of / stale_days (=None)。"""
    def boom(*_a, **_kw):
        raise RuntimeError("模拟组装失败")

    monkeypatch.setattr(global_risk, "_build", boom)
    global_risk.invalidate_cache()
    payload = global_risk.radar_snapshot(tmp_path, use_cache=False)
    assert payload["ok"] is False
    assert "as_of" in payload and payload["as_of"] is None
    assert "stale_days" in payload and payload["stale_days"] is None
    # fetched_at 保持「抓取时刻」语义, 不能被 as_of 顶掉
    assert payload["fetched_at"] != payload["as_of"]
    datetime.strptime(payload["fetched_at"], "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- premarket: 顶层 as_of


def test_premarket_top_as_of_is_today():
    """顶层 as_of = 今天 (简报问的是「今天开门怎么开」。)"""
    payload = premarket._build(allow_fetch=False)
    assert payload["as_of"] == global_risk._cn_today().isoformat()
    _assert_date_or_none(payload["as_of"])


def test_premarket_sub_objects_have_as_of_keys():
    """us / asia / a50 / cnh 四路都要带 as_of 键, 值可为 None。"""
    payload = premarket._build(allow_fetch=False)
    for key in ("us", "asia", "a50", "cnh"):
        assert key in payload, f"缺少子对象 {key}"
        assert "as_of" in payload[key], f"{key} 缺少 as_of"
        _assert_date_or_none(payload[key]["as_of"], field=f"{key}.as_of")


def test_premarket_degraded_payload_has_as_of(monkeypatch):
    """组装炸掉时降级 payload 的 as_of 为 None, 但键存在。"""
    def boom(*_a, **_kw):
        raise RuntimeError("模拟组装失败")

    monkeypatch.setattr(premarket, "_build", boom)
    premarket.invalidate_cache()
    payload = premarket.premarket_snapshot(use_cache=False)
    assert payload["ok"] is False
    assert "as_of" in payload and payload["as_of"] is None
    for key in ("us", "asia", "a50", "cnh"):
        assert "as_of" in payload[key]


def test_premarket_region_dates_pick_latest():
    """区域日期取该区域有效指数里最新的那天。"""
    assert premarket._latest_date([date(2026, 11, 1), date(2026, 11, 2)]) == date(2026, 11, 2)
    assert premarket._latest_date([None, date(2026, 11, 2)]) == date(2026, 11, 2)
    assert premarket._latest_date([None, None]) is None
    assert premarket._latest_date([]) is None
    assert premarket._iso(None) is None
    assert premarket._iso(date(2026, 11, 2)) == "2026-11-02"


def test_premarket_normal_payload_region_as_of(monkeypatch):
    """正常路径: 美股 as_of 是隔夜那天, 亚太 as_of 是同期那天, a50/cnh 是今天。

    用假的 bulk 数据把整条组装链路跑起来, 不联网。
    """
    today = global_risk._cn_today()
    overnight = (today - timedelta(days=1)).isoformat()
    today_iso = today.isoformat()

    # 新浪 gb_* 行: 名称, 现价, 涨跌%, 时间(第4列), ...
    # 时间是**北京时间**; 实测 "2026-11-04 06:30:00" 对应美股 11-03 收盘,
    # 所以这里给「今天 06:30」, 期望 us.as_of 回落成昨天 (隔夜交易日)。
    gb = ["纳斯达克", "25000.0", "-1.07", f"{today_iso} 06:30:00"]
    # 新浪 znb_* 行: 名称, 现价, 涨跌额, 涨跌%, 日期字符串, unix 时间戳
    znb = ["日经225", "63000.0", "-200.0", "-0.33", today_iso, "1759126320"]
    # 新浪 hf_* (A50 期货): 现价, ..., 昨收 在 [7]
    hf = ["12800.0", "0", "12801", "12799", "12810", "12790", today_iso, "12807.1", "12800"]
    # 新浪 fx_* (离岸人民币): 时间, 买价, 卖价, 昨收
    fx = [today_iso, "6.66", "6.67", "6.64"]

    bulk = {
        "gb_$ixic": gb,
        "gb_$dji": gb,
        "gb_$inx": gb,
        "znb_NKY": znb,
        "znb_KOSPI": znb,
        "znb_TWSE": znb,
        "hf_CHA50CFD": hf,
        "fx_susdcnh": fx,
    }
    monkeypatch.setattr(premarket, "_fetch_raw", lambda: bulk)
    monkeypatch.setattr(global_risk, "_em_quotes", lambda *_a, **_kw: {})
    monkeypatch.setattr(premarket, "_amplitude_from_local_etf", lambda: 1.1)

    payload = premarket._build(allow_fetch=True)
    assert payload["ok"] is True
    assert payload["as_of"] == today_iso
    assert payload["us"]["as_of"] == overnight
    assert payload["asia"]["as_of"] == today_iso
    assert payload["a50"]["as_of"] == today_iso
    assert payload["cnh"]["as_of"] == today_iso
    for key in ("us", "asia", "a50", "cnh"):
        _assert_date_or_none(payload[key]["as_of"], field=f"{key}.as_of")


def test_us_trading_date_shifts_before_noon():
    """美股 gb_* 的时间是北京时间: 中午 12 点前的时刻算前一个交易日。

    这是本轮最容易出错的一处 —— 若不做换算, 美股隔夜数据会被标成「今天」,
    用户依然分不清看的是今天还是昨天的行情。
    """
    assert global_risk._us_trading_date("2026-11-04 06:30:00") == date(2026, 11, 3)
    assert global_risk._us_trading_date("2026-11-04 05:35:05") == date(2026, 11, 3)
    # 中午之后不换算 (理论上不该出现, 但要确定边界稳定)
    assert global_risk._us_trading_date("2026-11-04 13:00:00") == date(2026, 11, 4)
    # 只有日期串时不做换算 —— 日期本身就是源方想表达的交易日
    assert global_risk._us_trading_date("2026-11-04") == date(2026, 11, 4)
    # 脏数据不炸
    assert global_risk._us_trading_date(None) is None
    assert global_risk._us_trading_date("") is None
    assert global_risk._us_trading_date("garbage") is None


def test_premarket_em_fallback_counts_as_today(monkeypatch):
    """东财兜底的数据没有日期字段, 按当天算 (与 global_risk 同一口径)。"""
    today_iso = global_risk._cn_today().isoformat()
    gb = ["纳斯达克", "25000.0", "-1.07", "2020-01-01 05:51:02"]
    # znb 给个一年前的日期 → 会被新鲜度校验丢掉 → 交给东财兜底
    old_znb = ["台湾加权", "44000.0", "-500.0", "-1.08", "2025-09-26", "1759126320"]

    bulk = {
        "gb_$ixic": gb,
        "gb_$dji": gb,
        "gb_$inx": gb,
        "znb_NKY": old_znb,
        "znb_KOSPI": old_znb,
        "znb_TWSE": old_znb,
        "hf_CHA50CFD": ["12800.0", "0", "1", "1", "1", "1", "t", "12807.1", "1"],
        "fx_susdcnh": [today_iso, "6.66", "6.67", "6.64"],
    }
    em = {
        "100.N225": {"name": "日经225", "price": 63000.0, "chg_pct": -0.33},
        "100.KS11": {"name": "韩国KOSPI", "price": 5700.0, "chg_pct": -0.5},
        "100.TWII": {"name": "台湾加权", "price": 44000.0, "chg_pct": -1.08},
    }
    monkeypatch.setattr(premarket, "_fetch_raw", lambda: bulk)
    monkeypatch.setattr(global_risk, "_em_quotes", lambda *_a, **_kw: em)
    monkeypatch.setattr(premarket, "_amplitude_from_local_etf", lambda: 1.1)

    payload = premarket._build(allow_fetch=True)
    assert payload["ok"] is True
    assert payload["asia"]["as_of"] == today_iso


# ---------------------------------------------------------------- fetched_at 不被污染


def test_fetched_at_stays_a_timestamp(tmp_path):
    """`fetched_at` 必须保持『抓取时刻』语义, 不能被 as_of 顶替成日期。"""
    radar = global_risk._build(tmp_path, "default", False)
    datetime.strptime(radar["fetched_at"], "%Y-%m-%d %H:%M:%S")

    pre = premarket._build(allow_fetch=False)
    datetime.strptime(pre["fetched_at"], "%Y-%m-%d %H:%M:%S")
    assert pre["as_of"] != pre["fetched_at"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
