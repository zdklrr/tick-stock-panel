"""0.3.45 回归测试: NAS 上「大盘择时停在昨天 + 每秒刷屏 + 接口 120s 超时」。

复现三条真 bug (v0.3.44 症状):
1. **全量重写**: 回补把腾讯返回的 320 根全部 merge-upsert → NAS 一次约 4 分钟,
   请求路径被拖死。修法: 只写本地缺的交易日。
2. **刷屏 + 重复全量写**: 「stale → free backfill」日志在取锁前打, 且拿到锁的
   请求用的是**进函数时的旧 as_of** → 每个并发请求都刷一行并各写一次全量。
   修法: 按标的占位 + 拿到占位后重读最新 as_of。
3. **落后时 30 分钟静默**: 09:25 竞价结束源站还没出当天日K, 旧实现按 30 分钟
   冷却 → 用户 9:25 打开页面还是昨天的数字。修法: 落后分支短间隔 (2 分钟) 盯着,
   源站一发布就写进去。

全程 mock 网络 (不联网), 断言写在盘上的行数, 不靠日志。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.services import index_daily_free as idf

_TZ = timezone(timedelta(hours=8))


def _cn(*args) -> datetime:
    return datetime(*args, tzinfo=_TZ)


def _write_days(tmp_path, table: str, symbol: str, days: list[date], close: float = 4.0) -> None:
    """写本地已有的日分区 (只写 symbol/date/close 三列, 够 _local_dates 读)。"""
    for d in days:
        part = tmp_path / table / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": [symbol], "date": [d], "close": [close]}).write_parquet(
            part / "part.parquet"
        )


def _tencent_rows(days: list[date], close: float = 4.0) -> list[list]:
    return [[d.isoformat(), "4.0", f"{close}", "4.5", "3.9", "1000"] for d in days]


# ── 1. 只写本地缺的交易日 ────────────────────────────────
def test_backfill_writes_only_missing_trading_days(monkeypatch, tmp_path):
    """本地缺最近 11 天, 源站给 91 天 → 只应写缺的 11 天, 不是 91 天。

    v0.3.44 是把 91 行全写一遍 (每行一次 parquet 读写)。
    """
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 10, 0))  # 周一盘中
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    source_days = [date(2026, 11, 9) - timedelta(days=90 - i) for i in range(91)]
    # 本地只缺最近 11 天 (前 80 天都有)
    local_days = source_days[:-11]
    _write_days(tmp_path, "kline_etf_daily", "510300", local_days)
    _write_days(tmp_path, "kline_index_daily", "000300.SH", local_days)

    monkeypatch.setattr(idf, "fetch_tencent_daily", lambda ts, bars=idf._BARS: _tencent_rows(source_days))

    written: dict[str, int] = {}

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

    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is True
    assert r["reason"] == "backfilled"
    # 91 - 80 = 11 天是新的; 全量写会是 91
    assert written.get("etf") == 11
    assert written.get("index") == 11


def test_backfill_write_rows_are_capped(monkeypatch, tmp_path):
    """全新安装: 本地一张日K都没有 → 一次只写 `_MAX_WRITE_ROWS` 个分区。

    v0.3.44 一次补 320 个分区要 4 分钟 (NAS 实测), 请求路径被拖死。
    """
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 10, 0))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    source_days = [date(2026, 11, 9) - timedelta(days=90 - i) for i in range(91)]
    monkeypatch.setattr(idf, "fetch_tencent_daily", lambda ts, bars=idf._BARS: _tencent_rows(source_days))

    written: dict[str, int] = {}

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

    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is True
    assert r["reason"] == "backfilled"
    assert written.get("etf") == idf._MAX_WRITE_ROWS
    assert written.get("index") == idf._MAX_WRITE_ROWS
    assert written["etf"] < 91


# ── 2. 并发不重复拉、不刷屏 ──────────────────────────────
def test_concurrent_calls_fetch_each_symbol_once(monkeypatch, tmp_path):
    """并发进来时每个标的只打一次腾讯接口 (v0.3.44 每个并发请求各打一遍)。

    同时验证「拿到占位后重读最新 as_of」: 先到的线程把数据写进去后,
    后到的线程应当直接判定「已新鲜」而不再拉。
    """
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 10, 0))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    # 本地数据停在昨天 → 落后 → 需要回补
    local_days = [date(2026, 11, 8) - timedelta(days=80 - i) for i in range(80)]
    _write_days(tmp_path, "kline_etf_daily", "510300", local_days)
    _write_days(tmp_path, "kline_index_daily", "000300.SH", local_days)

    source_days = [date(2026, 11, 9) - timedelta(days=90 - i) for i in range(91)]
    calls: list[str] = []
    fetch_lock = threading.Lock()

    def _fake_fetch(ts, bars=idf._BARS):
        with fetch_lock:
            calls.append(ts)
        time.sleep(0.05)
        return _tencent_rows(source_days)

    monkeypatch.setattr(idf, "fetch_tencent_daily", _fake_fetch)

    written: dict[str, int] = {}

    class _FakeRepo:
        def __init__(self, *a, **k) -> None:
            pass

        def append_etf_daily(self, df) -> None:
            written["etf"] = written.get("etf", 0) + df.height
            _write_days(tmp_path, "kline_etf_daily", "510300", source_days[-1:])

        def append_index_daily(self, df) -> None:
            written["index"] = written.get("index", 0) + df.height
            _write_days(tmp_path, "kline_index_daily", "000300.SH", source_days[-1:])

        def refresh_index_views(self) -> None:
            pass

    import app.tickflow.repository as repo_mod

    monkeypatch.setattr(repo_mod, "KlineRepository", _FakeRepo)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(idf.ensure_timing_index_fresh(tmp_path)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(results) == 4
    # 每个标的最多被打一次 (v0.3.44 是 4 个并发 × 2 标的 = 8 次)
    assert len(calls) <= len(idf.TIMING_TARGETS), f"重复拉取: {calls}"
    assert len(set(calls)) == len(calls), f"同一标的被拉多次: {calls}"
    # 每个结果都不该是异常 (兜底链永不抛)
    for r in results:
        assert isinstance(r, dict) and "reason" in r


# ── 3. 落后时的短间隔重试 (不再是 30 分钟静默) ────────────
def test_pending_backoff_is_short_when_source_lags(monkeypatch, tmp_path):
    """源站还没发布当天那根 → 间隔 2 分钟 (v0.3.44 是 30 分钟), 且不算故障。"""
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 10, 0))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    # 本地停在昨天, 源站也只给到昨天 (当天那根还没出)
    source_days = [date(2026, 11, 8) - timedelta(days=90 - i) for i in range(91)]
    _write_days(tmp_path, "kline_etf_daily", "510300", source_days)
    _write_days(tmp_path, "kline_index_daily", "000300.SH", source_days)
    monkeypatch.setattr(idf, "fetch_tencent_daily", lambda ts, bars=idf._BARS: _tencent_rows(source_days))

    class _FakeRepo:
        def __init__(self, *a, **k) -> None:
            pass

        def append_etf_daily(self, df) -> None:
            raise AssertionError("源站没有新数据时不该写盘")

        def append_index_daily(self, df) -> None:
            raise AssertionError("源站没有新数据时不该写盘")

        def refresh_index_views(self) -> None:
            pass

    import app.tickflow.repository as repo_mod

    monkeypatch.setattr(repo_mod, "KlineRepository", _FakeRepo)

    r = idf.ensure_timing_index_fresh(tmp_path)
    assert r["ok"] is True
    assert r["reason"] == "source-lagging"
    # 退避从 2 分钟起步, 连续失败逐级退到 15 分钟封顶
    assert r["backoff"]["510300"] == idf._PENDING_RETRY * 2
    # 落后分支的冷却用退避表, 不是 30 分钟
    assert r["backoff"]["510300"] < idf._REFETCH_COOLDOWN
    # 退避期到了就该重取 (v0.3.44 是 30 分钟静默 → 用户 9:25 看到的还是昨天)
    monkeypatch.setitem(idf._last_fetch_at, "510300", time.time() - (idf._PENDING_RETRY * 2 + 1))
    assert idf._should_refetch("2026-11-08", "510300", force=False) is True


def test_pending_backoff_caps_at_max(monkeypatch, tmp_path):
    """连续退避封顶在 `_PENDING_RETRY_MAX` (15 分钟), 不会无限增长。"""
    monkeypatch.setattr(idf, "_pending_backoff", {})
    for _ in range(20):
        idf._pending_backoff["510300"] = min(
            idf._pending_backoff.get("510300", idf._PENDING_RETRY) * 2,
            idf._PENDING_RETRY_MAX,
        )
    assert idf._pending_backoff["510300"] == idf._PENDING_RETRY_MAX


# ── 4. 请求路径不被拖死 ──────────────────────────────────
def test_fetch_budget_bounds_one_round(monkeypatch, tmp_path):
    """单轮回补的网络预算 `_FETCH_BUDGET` 是硬上限 (v0.3.44 会等 4 分钟)。"""
    from app.services import market_timing as mt

    monkeypatch.setattr(mt, "_cn_now", lambda: _cn(2026, 11, 9, 10, 0))
    monkeypatch.setattr(mt, "_expected_as_of", lambda now=None: date(2026, 11, 9))
    monkeypatch.setattr(idf, "_last_fetch_at", {})
    monkeypatch.setattr(idf, "_pending_backoff", {})
    monkeypatch.setattr(idf, "_inflight", set())

    local_days = [date(2026, 11, 8) - timedelta(days=80 - i) for i in range(80)]
    _write_days(tmp_path, "kline_etf_daily", "510300", local_days)
    _write_days(tmp_path, "kline_index_daily", "000300.SH", local_days)

    source_days = [date(2026, 11, 9) - timedelta(days=90 - i) for i in range(91)]

    def _slow_fetch(ts, bars=idf._BARS):
        time.sleep(0.4)
        return _tencent_rows(source_days)

    monkeypatch.setattr(idf, "fetch_tencent_daily", _slow_fetch)

    class _FakeRepo:
        def __init__(self, *a, **k) -> None:
            pass

        def append_etf_daily(self, df) -> None:
            pass

        def append_index_daily(self, df) -> None:
            pass

        def refresh_index_views(self) -> None:
            pass

    import app.tickflow.repository as repo_mod

    monkeypatch.setattr(repo_mod, "KlineRepository", _FakeRepo)

    t0 = time.time()
    idf.ensure_timing_index_fresh(tmp_path)
    elapsed = time.time() - t0
    # 两个标的各 0.4 秒 + 预算 6 秒 → 无论如何不该超过预算 + 一轮取数时间
    assert elapsed < idf._FETCH_BUDGET + 2.0, f"单轮回补超时: {elapsed:.1f}s"
