"""免费指数日K兜底同步 (index_daily_free) 测试。

覆盖: 腾讯行解析 / 脏行过滤 / 滞后天数解析 / 新鲜即跳过 / 回补写盘后 trend_timing
恢复 / fetch 失败软降级不抛。全程 mock 网络函数 —— 测试不联网。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.services import index_daily_free as idf

_TZ = timezone(timedelta(hours=8))


def _cn(*args) -> datetime:
    """北京时间 datetime (与 market_time.CN_TZ 一致)。"""
    return datetime(*args, tzinfo=_TZ)


# ── 腾讯行 → frame ─────────────────────────────────────
def test_to_kline_frame_parses_rows():
    rows = [
        ["2026-11-03", "4005.25", "4008.87", "4028.74", "3969.55", "248779950"],
        ["2026-11-04", "4007.35", "4011.22", "4032.84", "3987.15", "124969996"],
    ]
    f = idf.to_kline_frame(rows, "000300.SH")
    assert f.height == 2
    assert f["symbol"].to_list() == ["000300.SH", "000300.SH"]
    assert f["date"].to_list() == [date(2026, 11, 3), date(2026, 11, 4)]
    # 腾讯列序: [date, open, close, high, low, volume]
    assert f["close"].to_list() == [4008.87, 4011.22]
    assert f["open"].to_list() == [4005.25, 4007.35]
    assert f["amount"].to_list() == [0.0, 0.0]


def test_to_kline_frame_filters_bad_rows():
    rows = [
        ["2026-11-03", "4005.25", "0", "4028.74", "3969.55", "1"],        # close<=0 丢
        ["2026-11-04", "x", "y", "z", "w", "1"],                          # 非法数值丢
    ]
    f = idf.to_kline_frame(rows, "000300.SH")
    assert f.height == 0
    assert f.schema == idf._KLINE_SCHEMA


def test_to_kline_frame_short_row_volume_default():
    # 只有 5 列 (缺 volume) 的行不炸, volume 兜底 0.0 —— 腾讯偶发短行
    f = idf.to_kline_frame([["2026-11-05", "4007", "4011", "4032", "3987"]], "000300.SH")
    assert f.height == 1
    assert f["volume"].to_list() == [0.0]


def test_normalize_suffix():
    assert idf._normalize_suffix("sh000300") == "000300.SH"
    assert idf._normalize_suffix("sz399001") == "399001.SZ"
    assert idf._normalize_suffix("510300") == "510300"


def test_stale_days_text():
    today = datetime.now().date()
    assert idf._stale_days_text(today.isoformat()) == 0
    assert idf._stale_days_text((today - timedelta(days=3)).isoformat()) == 3
    assert idf._stale_days_text(None) == 999
    assert idf._stale_days_text("") == 999
    assert idf._stale_days_text("garbage") == 999


def test_behind_expected_uses_trading_days(monkeypatch):
    """回补判据走交易日口径: 盘后昨日数据算落后, 盘中不算 (v0.3.40)。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from app.services import market_timing as mt

    tz = _tz(_td(hours=8))

    def _fake_now():
        return _dt(2026, 11, 5, 15, 35, tzinfo=tz)

    monkeypatch.setattr(mt, "_cn_now", _fake_now)
    assert idf._behind_expected("2026-11-04") is True   # 收盘后: 应有 11-05
    assert idf._behind_expected("2026-11-05") is False  # 当天就有 → 不落后
    assert idf._behind_expected(None) is True           # 没数据 → 要回补
    assert idf._behind_expected("garbage") is True

    def _fake_now_intraday():
        return _dt(2026, 11, 5, 10, 0, tzinfo=tz)

    monkeypatch.setattr(mt, "_cn_now", _fake_now_intraday)
    assert idf._behind_expected("2026-11-04") is True   # v0.3.41: 盘中当日K 也该有了

    def _fake_now_preopen():
        return _dt(2026, 11, 5, 9, 0, tzinfo=tz)

    monkeypatch.setattr(mt, "_cn_now", _fake_now_preopen)
    assert idf._behind_expected("2026-11-04") is False  # 开盘前: 昨日就是最新


# ── ensure_timing_index_fresh ─────────────────────────
def test_fresh_data_short_circuits(monkeypatch, tmp_path):
    """开盘前: 昨日就是最新 → 不联网。

    时间必须钉死: 盘中/盘后当日那根 K 要重取 (v0.3.41), 用 realtime 跑这个用例
    会随跑测的时刻翻脸。这里钉在周一 09:00 (竞价前), 应有交易日 = 上周五。
    """
    called = {"n": 0}

    def _fake_fetch(ts_symbol, bars=idf._BARS):
        called["n"] += 1
        raise AssertionError("fresh 数据不应触发网络拉取")

    monkeypatch.setattr(idf, "fetch_tencent_daily", _fake_fetch)
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 9, 0))  # 周一竞价前

    latest = date(2026, 11, 6)  # 上周五
    closes = [4.0 + 0.01 * i for i in range(80)]
    days = [latest - timedelta(days=79 - i) for i in range(80)]
    table = tmp_path / "kline_etf_daily"
    for d, c in zip(days, closes, strict=True):
        part = table / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["510300"], "date": [d], "close": [float(c)]}).write_parquet(
            part / "part.parquet"
        )
    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is True
    assert r["fetched"] == {}
    assert r["reason"] == "fresh"
    assert called["n"] == 0


def test_backfill_recovers_stale_timing(monkeypatch, tmp_path):
    """滞后数据 → 免费回补写盘 → trend_timing 恢复最新。全程 mock 网络。"""
    today = datetime.now().date()
    # 旧数据: 停在 6 天前, 80 根
    stale_end = today - timedelta(days=6)
    days = [stale_end - timedelta(days=80 - i) for i in range(80)]
    table = tmp_path / "kline_etf_daily"
    for i, d in enumerate(days):
        part = table / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["510300"], "date": [d], "close": [4.0 + 0.01 * i]}).write_parquet(
            part / "part.parquet"
        )

    def _fake_fetch(ts_symbol, bars=idf._BARS):
        # 返回含今天的 91 根 (today-90 .. today)
        return [
            [(today - timedelta(days=90 - i)).isoformat(), "4.0", f"{4.0 + 0.01 * i}", "4.5", "3.9", "1000"]
            for i in range(91)
        ]

    monkeypatch.setattr(idf, "fetch_tencent_daily", _fake_fetch)
    # 隔离冷却时间戳
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is True
    assert r["reason"] == "backfilled"
    # ETF 目标代码无后缀 (与 market_timing 主候选一致), 指数带 .SH
    assert set(r["fetched"]) == {"000300.SH", "510300"}
    # v0.3.45: **只写本地缺的交易日**, 不是把源站的 91 根全写一遍。
    # 本地有 80 天 (停在 6 天前), 源站给 91 天 → 缺 11 天 → 只写 11 行。
    # 指数表本地一张都没有 (全新安装) → 全量也受 `_MAX_WRITE_ROWS` 限流, 只写 20 行。
    assert r["fetched"]["510300"] == 11
    assert r["fetched"]["000300.SH"] == idf._MAX_WRITE_ROWS

    # 回补后 trend_timing 应恢复到今天
    from app.services import market_timing as mt

    t = mt.trend_timing(tmp_path)
    assert t["ok"] is True
    assert t["as_of"] == today.isoformat()
    assert t["stale_days"] == 0
    assert t["bars"] >= mt.MIN_BARS


def test_fetch_empty_soft_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(idf, "fetch_tencent_daily", lambda *a, **k: [])
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    r = idf.ensure_timing_index_fresh(tmp_path)
    # v0.3.45: 源站取不到数据 = 真故障 → ok=False。
    # (源站有数据、只是还没发布当天那根 → 走 "source-lagging", ok=True 退避重试)
    assert r["ok"] is False
    assert r["reason"] == "fetch-empty"


def test_internal_error_never_raises(monkeypatch, tmp_path):
    def _boom(data_dir=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.market_timing.trend_timing", _boom)
    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is False
    assert "error" in r["reason"]


def test_cooldown_prevents_refetch(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setattr(idf, "_last_fetch_at", {"000300.SH": _time.time(), "510300": _time.time()})
    fetched = {"n": 0}

    def _fake_fetch(ts_symbol, bars=idf._BARS):
        fetched["n"] += 1
        return [["2026-11-04", "1", "2", "3", "0.5", "10"]]

    monkeypatch.setattr(idf, "fetch_tencent_daily", _fake_fetch)
    # 数据滞后 (as_of 老于 1 天: 停在 10 天前) 但刚拉过 → 冷却期内不再拉
    stale_days = [(datetime.now().date() - timedelta(days=90 - i)) for i in range(80)]
    table = tmp_path / "kline_etf_daily"
    for i, d in enumerate(stale_days):
        part = table / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["510300"], "date": [d], "close": [4.0 + 0.01 * i]}).write_parquet(
            part / "part.parquet"
        )
    r = idf.ensure_timing_index_fresh(tmp_path)
    assert fetched["n"] == 0
    # 冷却期内不拉 → 不是故障: ok=True + reason="cooling" (v0.3.41 起不再报 fetch-empty,
    # 否则 refresh_market_context 会把「刚拉过」记成一条 error)
    assert r["ok"] is True
    assert r["reason"] == "cooling"
    assert r["fetched"] == {}
    assert set(r["cooldown"]) == {"000300.SH", "510300"}


def test_intraday_bar_refetches_with_short_cooldown(monkeypatch):
    """v0.3.41: as_of 已是今天但还在盘中 → 5 分钟内才跳过, 之后要重取。

    不含这段逻辑的话, 盘中一拿到当天K 就再也不会更新, 卡片冻结在上午的价格。
    """
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 5, 10, 30))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 5))

    # 刚拉过 (2 分钟前) → 盘中冷却 5 分钟未到, 不重取
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - 120)
    assert idf._should_refetch("2026-11-05", "510300", force=False) is False

    # 6 分钟前拉过 → 盘中该重取
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - 360)
    assert idf._should_refetch("2026-11-05", "510300", force=False) is True

    # 收盘后 (16:00) 且当天只拉过一次盘中 → 也要补一次定型
    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 5, 16, 0))
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - 3600)
    assert idf._should_refetch("2026-11-05", "510300", force=False) is True


def test_intraday_flag_soft_when_settled(monkeypatch):
    """收盘后 as_of=今天 且收盘后已定型过 → 不再重取 (走 30 分钟冷却)。"""
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 5, 16, 0))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 5))
    # 收盘后 (15:50) 已经抓过 → 定型完成, 10 分钟内不再重取
    monkeypatch.setitem(idf._last_fetch_at, "510300", _cn(2026, 11, 5, 15, 50).timestamp())
    assert idf._should_refetch("2026-11-05", "510300", force=False) is False