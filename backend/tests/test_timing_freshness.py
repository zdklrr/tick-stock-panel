"""0.3.45 回归测试: 竞价后打开页面, 大盘择时/情绪周期必须已经是当天。

复现用户 m20840 的场景: 「今天竞价已经结束了, 我看 NAS 上的看盘界面,
情绪周期、大盘择时还是 5 号的」。三条修法各自锁一条:

1. **同步自愈必须有界** —— `market_timing.trend_timing` 的同步回补每个标的最多写
   `_SYNC_MAX_WRITE_ROWS` 个日分区 (v0.3.44 是 320 个 → NAS 一次 4 分钟 →
   `/api/risk/timing` 与 `/api/risk/advice` 120 秒超时)。
2. **落后时的重试间隔要短** —— 09:25 竞价结束时源站往往还没出当天那根日K,
   旧实现 30 分钟冷却 → 用户 9:25 打开页面还是昨天的数字。
3. **盘中补算节奏要快** —— `main.market-context-catchup` 在当日数据还在变的
   窗口 (9:15-15:05) 里 3 分钟复查一次, 窗口外 30 分钟一次。

全程 mock 网络, 不联网。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.services import index_daily_free as idf
from app.services import market_timing as mt

_TZ = timezone(timedelta(hours=8))


def _cn(*args) -> datetime:
    return datetime(*args, tzinfo=_TZ)


def _write_days(tmp_path, table: str, symbol: str, days: list[date], close: float = 4.0) -> None:
    for d in days:
        part = tmp_path / table / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": [symbol], "date": [d], "close": [close]}).write_parquet(
            part / "part.parquet"
        )


def _tencent_rows(days: list[date], close: float = 4.0) -> list[list]:
    return [[d.isoformat(), "4.0", f"{close}", "4.5", "3.9", "1000"] for d in days]


def _fake_repo(monkeypatch, written: dict[str, int]) -> None:
    class _FakeRepo:
        def __init__(self, *a, **k) -> None:
            pass

        def append_etf_daily(self, df) -> None:
            written["etf"] = written.get("etf", 0) + df.height

        def append_index_daily(self, df) -> None:
            written["index"] = written.get("index", 0) + df.height

        def refresh_index_views(self) -> None:
            pass

    import app.tickflow.repository as repo_mod

    monkeypatch.setattr(repo_mod, "KlineRepository", _FakeRepo)


# ── 1. 同步自愈有界 ──────────────────────────────────────
def test_sync_heal_writes_only_a_few_partitions(monkeypatch, tmp_path):
    """同步回补每个标的最多写 `_SYNC_MAX_WRITE_ROWS` 个分区。

    v0.3.44: 同步路径把腾讯返回的 320 根全写一遍 → NAS 一次约 4 分钟 →
    `/api/risk/timing` 直接 120 秒超时。
    """
    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 9, 30))  # 周一竞价刚结束
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    source_days = [date(2026, 11, 9) - timedelta(days=90 - i) for i in range(91)]
    # 本地一张日K都没有 (全新安装) → 源站 91 天全是新的
    monkeypatch.setattr(idf, "fetch_tencent_daily", lambda ts, bars=idf._BARS: _tencent_rows(source_days))

    written: dict[str, int] = {}
    _fake_repo(monkeypatch, written)

    idf.ensure_timing_index_fresh(tmp_path, max_rows=mt._SYNC_MAX_WRITE_ROWS)

    assert written.get("etf") == mt._SYNC_MAX_WRITE_ROWS
    assert written.get("index") == mt._SYNC_MAX_WRITE_ROWS
    assert written["etf"] < 91, "同步路径不该全量写"


def test_trend_timing_sync_heal_is_bounded(monkeypatch, tmp_path):
    """`trend_timing` 的同步自愈必须把 max_rows 传下去 (有界)。"""
    from app import config

    prod = tmp_path / "prod_data"
    prod.mkdir()
    monkeypatch.setattr(config.settings, "data_dir", prod, raising=False)
    monkeypatch.setattr(mt, "_is_healable_dir", lambda _d: True)
    monkeypatch.setattr(mt, "_is_behind_expected", lambda as_of, now=None: True)
    monkeypatch.setattr(mt, "_intraday_kicked_at", 0.0, raising=False)
    monkeypatch.setattr(mt, "_intraday_thread", None, raising=False)

    seen: dict[str, object] = {}

    def _fake_heal(data_dir=None, *, force=False, max_rows=None):
        seen["max_rows"] = max_rows
        return {"ok": True, "fetched": {}, "reason": "source-lagging"}

    from app.services import index_daily_free as idf_mod

    monkeypatch.setattr(idf_mod, "ensure_timing_index_fresh", _fake_heal)
    # 同步自愈之后 trend_timing 还会起后台线程继续补剩余缺口 —— 那次调用
    # 刻意不带 max_rows (后台用默认 20), 会把 seen["max_rows"] 覆盖成 None。
    # 后台行为已有专门用例 (test_trend_timing_kicks_background_after_sync_heal),
    # 这里桩掉以免污染本用例要验证的「同步调用是否传了 max_rows」。
    monkeypatch.setattr(mt, "_kick_intraday_refresh", lambda _d: None)

    mt.trend_timing(prod)

    assert seen["max_rows"] == mt._SYNC_MAX_WRITE_ROWS


def test_trend_timing_kicks_background_after_sync_heal(monkeypatch, tmp_path):
    """同步只补最近一截 → 剩下的缺口交给后台线程补齐。"""
    from app import config

    prod = tmp_path / "prod_data"
    prod.mkdir()
    monkeypatch.setattr(config.settings, "data_dir", prod, raising=False)
    monkeypatch.setattr(mt, "_is_healable_dir", lambda _d: True)
    monkeypatch.setattr(mt, "_is_behind_expected", lambda as_of, now=None: True)
    monkeypatch.setattr(mt, "_intraday_kicked_at", 0.0, raising=False)
    monkeypatch.setattr(mt, "_intraday_thread", None, raising=False)

    from app.services import index_daily_free as idf_mod

    monkeypatch.setattr(
        idf_mod,
        "ensure_timing_index_fresh",
        lambda data_dir=None, **_kw: {"ok": True, "fetched": {}, "reason": "source-lagging"},
    )

    kicked: list[str] = []
    monkeypatch.setattr(mt, "_kick_intraday_refresh", lambda d: kicked.append(str(d)))

    mt.trend_timing(prod)

    assert len(kicked) == 1, "同步回补之后应起后台线程继续补"


# ── 2. 落后时短间隔重试 ──────────────────────────────────
def test_behind_expected_retries_within_two_minutes(monkeypatch):
    """落后分支的重试间隔必须短于 5 分钟 (源站出当天那根就写进去)。"""
    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 9, 30))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_pending_backoff", {})

    # 刚拉过 (30 秒前) → 短间隔内不重拉
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - 30)
    assert idf._should_refetch("2026-11-08", "510300", force=False) is False

    # 超过退避间隔 (2 分钟 + 1) → 该重拉 (v0.3.44 要等 30 分钟)
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - (idf._PENDING_RETRY + 1))
    assert idf._should_refetch("2026-11-08", "510300", force=False) is True


# ── 3. 盘中补算节奏 ──────────────────────────────────────
def test_data_refresh_window_covers_auction_to_close():
    """当日数据还在变的窗口: 竞价 09:15 → 收盘定型 15:05。"""
    from app.market_time import in_data_refresh_window

    assert in_data_refresh_window(_cn(2026, 11, 9, 9, 14)) is False  # 竞价前
    assert in_data_refresh_window(_cn(2026, 11, 9, 9, 25)) is True   # 竞价结束
    assert in_data_refresh_window(_cn(2026, 11, 9, 10, 0)) is True
    assert in_data_refresh_window(_cn(2026, 11, 9, 15, 5)) is True   # 收盘定型
    assert in_data_refresh_window(_cn(2026, 11, 9, 15, 6)) is False  # 收盘后
    assert in_data_refresh_window(_cn(2026, 11, 7, 10, 0)) is False  # 周六


def test_market_context_loop_polls_faster_inside_window():
    """后台补算的复查间隔: 窗口内 3 分钟, 窗口外 30 分钟。"""
    from app.market_time import data_refresh_interval_seconds

    # 竞价结束到收盘定型之间 → 3 分钟
    assert data_refresh_interval_seconds(_cn(2026, 11, 9, 9, 30)) == 180
    assert data_refresh_interval_seconds(_cn(2026, 11, 9, 14, 0)) == 180
    # 收盘之后 / 开盘之前 / 周末 → 30 分钟
    assert data_refresh_interval_seconds(_cn(2026, 11, 9, 15, 30)) == 1800
    assert data_refresh_interval_seconds(_cn(2026, 11, 9, 9, 0)) == 1800
    assert data_refresh_interval_seconds(_cn(2026, 11, 7, 10, 0)) == 1800
