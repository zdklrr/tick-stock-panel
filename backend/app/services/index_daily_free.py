"""免费指数日K兜底同步 (v0.3.35)。

背景 (#510): NAS 的 tickflow key 过期后 sync_index 被静默 skip, 指数/ETF 日K停更,
大盘择时 (market_timing) 与情绪周期同源停更 —— 000300.SH 停在旧日期, 而 510300.SH
只有零星几根, 远不够 MA60。风险雷达/盘前简报走实时免费接口不受影响, 于是界面出现
"只有风险雷达是最新日期" 的现象。

本模块提供一条**不依赖 tickflow key** 的兜底链: 腾讯公开日K接口拉沪深300 指数与
510300 ETF 各 320 根, 直接写 kline_index_daily / kline_etf_daily 分区, 让
market_timing.trend_timing 恢复到最新交易日。

设计约束:
- 永不抛异常 (软失败): 兜底链任何一环失败都只打日志, 不影响调用方;
- 幂等: append_*_daily 内部按日分区 merge-upsert, 重复拉取只覆盖同日数据;
- 节流: 同一 symbol 回补成功后 30 分钟内不重复拉 (内存时间戳);
- 只补择时链路需要的两个标的 (000300.SH / 510300.SH), 不做全量指数回补 ——
  全量属 TickFlow/自定义源的正路职责。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

_TENCENT_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    "?param={ts},day,,,{bars},"
)
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_TIMEOUT = 10.0
_BARS = 320
#: 同一标的回补成功后的静默期 (秒), 避免频繁读取时反复打腾讯接口
_REFETCH_COOLDOWN = 30 * 60

#: 盘中 (当日那根K还在跳动) 的重取间隔 (秒)。收盘定型前这根 K 一直在变,
#: 只靠「as_of 是否落后」判断会在拿到当天之后就永不再取 → 卡片冻结在上午的价格。
_INTRADAY_COOLDOWN = 5 * 60

#: **落后期望交易日** 时的重取间隔 (秒) —— 与上面两个不同, 必须短。
#:
#: 腾讯在连续竞价开始 (09:30) 前后才陆续发布当天那根日K; 09:25 竞价结束时
#: 源站给不出当天那根, 于是本地永远「落后」。旧实现按 `_REFETCH_COOLDOWN`
#: (30 分钟) 静默, 结果用户 9:25 打开页面看到的还是昨天的数字, 要等半小时。
#: 这里改成短间隔盯着 —— 源站一发布就写进去。
_PENDING_RETRY = 120

#: 单次回补的网络预算 (秒): 超支就不再取下一个标的。
#: 保证「请求路径同步等回补」最多等这么久, 不会像 v0.3.44 那样把
#: `/api/risk/timing` 拖到 120 秒超时。
_FETCH_BUDGET = 6.0

#: 单轮最多写多少个日分区。回补只写本地缺的交易日, 但**全新安装**时本地
#: 一张日K都没有 → 一次补 320 个分区要 4 分钟 (NAS 实测)。限流后每轮补
#: 一批最新的, 几轮就攒够 MA60 需要的 60 根。
_MAX_WRITE_ROWS = 20

#: trend_timing 候选标的: (本地 symbol 带后缀, 腾讯 ts 代码, 写哪张表)。
#: 注意 ETF 用**无后缀** 510300 —— 与 market_timing.SYMBOL_CANDIDATES 的
#: ETF 主候选一致 (那才是择时真正读的代码)。
TIMING_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("510300", "sh510300", "etf"),
    ("000300.SH", "sh000300", "index"),
)

#: 目标标的 → 写哪张原始日K表 (回补只碰这两张)
_TARGET_TABLE = {"etf": "kline_etf_daily", "index": "kline_index_daily"}

_last_fetch_at: dict[str, float] = {}

#: 标的 → 当前「落后期望交易日」情形的重取间隔 (秒)。
#:
#: 源站连续几次都还没给出应有的那根日K (比如腾讯 09:30 前不出当天日K), 这时
#: 间隔逐级退避, 避免每 2 分钟就把同一个接口打一遍还打不出东西。
#: 一旦写进去了 (as_of 前进), 退避表清空。
_pending_backoff: dict[str, float] = {}
_PENDING_RETRY_MAX = 15 * 60

#: 同一标的同一时刻只允许一个线程在回补。
#:
#: v0.3.44 用一把全局 `_fetch_lock` 把**网络请求也裹在里面**, 于是:
#: ①并发请求排在锁上, 每个都先打一行「stale → free backfill」造成刷屏;
#: ②拿到锁的请求用的是**进函数时的旧 as_of**, 判定依旧落后 → 又一次全量重写。
#: 现在改成「按标的占位 + 不持锁做网络请求」: 同一标的重复请求直接跳过,
#: 不同标的可以并行, 网络与写盘都不在全局锁内。
_inflight: set[str] = set()
_inflight_lock = threading.Lock()

_KLINE_SCHEMA = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "quote_ts": pl.Int64,
}


def _normalize_suffix(symbol: str) -> str:
    """sh000300 → 000300.SH / sz399001 → 399001.SZ (与本地维表口径一致)."""
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:] + ".SH"
    if s.startswith("sz"):
        return s[2:] + ".SZ"
    return s.upper()


def fetch_tencent_daily(ts_symbol: str, bars: int = _BARS) -> list[list]:
    """腾讯日K: 返回 [date, open, close, high, low, volume] 行数组; 失败返 []."""
    url = _TENCENT_KLINE_URL.format(ts=ts_symbol, bars=bars)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read())
        node = (payload.get("data") or {}).get(ts_symbol) or {}
        rows = node.get("day") or node.get("qfqday") or []
        return rows if isinstance(rows, list) else []
    except Exception as e:  # noqa: BLE001 — 兜底链永不抛
        logger.warning("tencent kline fetch failed (%s): %s", ts_symbol, e)
        return []


def to_kline_frame(rows: list[list], symbol: str) -> pl.DataFrame:
    """腾讯 day 行 → 内部日K契约 frame (列序 date,open,close,high,low,volume)。"""
    recs: list[dict] = []
    for r in rows:
        try:
            d = str(r[0])
            o, c, h, low = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            vol = float(r[5]) if len(r) > 5 else 0.0
        except (ValueError, TypeError, IndexError):
            continue
        if c <= 0:
            continue
        recs.append(
            {
                "symbol": symbol,
                "date": datetime.strptime(d, "%Y-%m-%d").date(),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": vol,
                "amount": 0.0,
                "quote_ts": int(time.time()),
            }
        )
    if not recs:
        return pl.DataFrame(schema=_KLINE_SCHEMA)
    return pl.DataFrame(recs, schema=_KLINE_SCHEMA)


def _stale_days_text(as_of: str | None) -> int:
    """as_of 距今天的自然日数; 解析失败返 999 (视为严重滞后)。

    **仅用于日志/展示**。是否回补看 `_behind_expected` —— 盘后 as_of 停在昨日
    时自然日差恒为 1, 用它当新鲜判据会导致整个晚上都不回补 (v0.3.40 修)。
    """
    if not as_of:
        return 999
    try:
        d = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except ValueError:
        return 999


def _parse_day(value: str | None) -> date | None:
    """'2026-11-06' → date(2026,11,6); 解析不了返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _behind_expected(as_of: str | None) -> bool:
    """as_of 是否落后于此刻应有的交易日 (按交易日口径, 不联网)。"""
    from app.services.market_timing import _is_behind_expected

    return _is_behind_expected(as_of)


def _should_refetch(as_of: str | None, symbol: str, *, force: bool) -> bool:
    """该不该重取这个标的。

    - force → 总是取;
    - as_of 落后应有交易日 → 取, 间隔按「源站还没发布当天那根」的情形取短
      (`_pending_backoff`, 默认 `_PENDING_RETRY` 2 分钟, 连续不前进则退避到
      `_PENDING_RETRY_MAX` 15 分钟);
    - as_of 已是当天但**还在盘中** → 按 `_INTRADAY_COOLDOWN` (5 分钟) 重取,
      因为当日那根K 还没定型, 不重取就会冻结在上午的价格上;
    - as_of 是当天、已过收盘, 但收盘后还没定过型 → 取一次拿收盘价
      (盘中最后一次抓的 K 缺尾盘, 不定型指标会一直偏差)。
    冷却用 `_last_fetch_at` 这一个表, 间隔按场景取不同值。

    v0.3.45: 「落后」这一支原来用 30 分钟冷却 —— 09:25 竞价结束时腾讯往往还
    拿不出当天那根日K, 于是用户 9:25 打开页面看到的还是昨天的数字, 要再等
    半小时才更新。改成短间隔盯着, 源站一发布就写进去。
    """
    now = time.time()
    last = _last_fetch_at.get(symbol, 0.0)
    intraday = _is_intraday_bar(as_of)
    if force:
        return True
    if _is_settle_refetch_needed(as_of, symbol, now):
        return True
    behind = _behind_expected(as_of)
    if behind:
        cooldown = _pending_backoff.get(symbol, _PENDING_RETRY)
    elif intraday:
        cooldown = _INTRADAY_COOLDOWN
    else:
        cooldown = _REFETCH_COOLDOWN
    if last and now - last < cooldown:
        return False
    return behind or intraday


def _local_dates(data_dir, table: str, symbol: str) -> set[date]:
    """本地该标的已有哪些交易日 (读 date+symbol 两列的惰性扫描)。

    回补只写「本地缺的交易日」—— v0.3.44 每次把腾讯返回的 320 根全部
    merge-upsert, 一次要 320 次 parquet 读写 (NAS 上约 4 分钟), 请求路径被拖死。
    """
    base = Path(data_dir) / table
    if not base.exists():
        return set()
    try:
        from app.parquet import scan_daily_parquet

        df = (
            scan_daily_parquet((base / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol") == symbol)
            .select("date")
            .unique()
            .collect()
        )
    except Exception as e:  # noqa: BLE001 — 读不到就当「全都要写」(保守)
        logger.warning("读取本地 %s/%s 日期失败 (按全量回补处理): %s", table, symbol, e)
        return set()
    out: set[date] = set()
    for d in df["date"].to_list():
        if isinstance(d, date):
            out.add(d)
        else:
            try:
                out.add(date.fromisoformat(str(d)[:10]))
            except ValueError:
                continue
    return out


def _claim_symbol(symbol: str) -> bool:
    """占位: 同一标的已有线程在回补就返回 False (不再打同一个接口)。"""
    with _inflight_lock:
        if symbol in _inflight:
            return False
        _inflight.add(symbol)
        return True


def _release_symbol(symbol: str) -> None:
    with _inflight_lock:
        _inflight.discard(symbol)



def _is_settle_refetch_needed(as_of: str | None, symbol: str, now: float) -> bool:
    """当天数据是盘中抓的、收盘后还没再抓过 → 需要补一次定型。

    盘中抓到的那根 K 缺尾盘 (成交量/收盘价都不是最终的), 不补一次指标会一直偏差。
    判据: as_of 已是今天 + 现在已过 15:05 + 上次抓取发生在今天 15:05 之前。
    """
    from app.market_time import CN_TZ
    from app.services.market_timing import _CLOSE_READY, _cn_now, _expected_as_of

    today = _expected_as_of()
    if as_of != today.isoformat():
        return False
    if _cn_now().time() < _CLOSE_READY:
        return False
    last = _last_fetch_at.get(symbol, 0.0)
    if not last:
        return True
    last_dt = datetime.fromtimestamp(last, tz=CN_TZ)
    if last_dt.date() != today:
        return True
    return last_dt.time() < _CLOSE_READY


def _is_intraday_bar(as_of: str | None) -> bool:
    """as_of 是否就是当前这根**未定型**的盘中K (转调 market_timing 口径)。"""
    from app.services.market_timing import _is_intraday_bar as _impl

    return _impl(as_of)


def ensure_timing_index_fresh(data_dir=None, *, force: bool = False, max_rows: int | None = None) -> dict:
    """大盘择时数据自愈入口: 000300/510300 日K落后于应有交易日时免费回补。

    返回 {"ok": bool, "fetched": {symbol: rows}, "reason": str}。
    - 数据已新鲜 → ok=True, fetched={}, reason="fresh"
    - 回补成功 → ok=True, fetched={symbol: 写入行数}
    - 回补失败 → ok=False, reason=失败说明 (不影响调用方)
    force=True 时跳过新鲜判断与冷却, 直接重拉 (供手动触发/测试)。
    max_rows: 单个标的单轮最多写几个日分区 (None → `_MAX_WRITE_ROWS`)。
      请求路径用小值 (见 market_timing._SYNC_MAX_WRITE_ROWS), 保证同步回补
      不会把 HTTP 请求拖死; 后台补算用默认值, 慢慢把缺失的交易日攒齐。

    v0.3.45 修的三处 (NAS 上「择时停在昨天 + 每秒刷屏 + 接口 120s 超时」):
    1. **只写本地缺的交易日** —— 原来把腾讯返回的 320 根全部 merge-upsert,
       一次要 320 次 parquet 读写 (NAS 上约 4 分钟), 请求全堆在锁上。
    2. **日志与判定都放进占位之后** —— 原来「stale → free backfill」在取锁前打,
       每个并发请求都打一行; 而且拿到锁的请求用的是**进函数时的旧 as_of**,
       判定依旧落后 → 又一次全量重写。
    3. **网络有预算** —— 单轮最多 `_FETCH_BUDGET` 秒, 请求路径不会被拖死。
    """
    fetched: dict[str, int] = {}
    try:
        from app.services.market_timing import trend_timing

        # _heal=False: 只读不触发 market_timing 的自愈 (避免互相递归)
        timing = trend_timing(data_dir, _heal=False)
        as_of = timing.get("as_of") if isinstance(timing, dict) else None
        stale = _stale_days_text(as_of)
        intraday = _is_intraday_bar(as_of)
        behind = _behind_expected(as_of)
        # v0.3.40: 用交易日口径判断是否要回补 (自然日 > 1 在盘后永远不成立)
        # v0.3.41: 盘中那根K 未定型, 到点也要重取 (否则冻结在上午的价格)
        if not force and not behind and not intraday:
            return {"ok": True, "fetched": {}, "reason": "fresh"}

        from app.tickflow.repository import DataStore, KlineRepository

        repo = KlineRepository(DataStore(data_dir))
        skipped: dict[str, float] = {}
        busy: list[str] = []
        attempted: list[str] = []
        fetch_failed = False
        deadline = time.time() + _FETCH_BUDGET
        for symbol, ts_symbol, kind in TIMING_TARGETS:
            # 判定放在占位之后: 并发进来时只有第一个线程会真的去拉。
            if not _claim_symbol(symbol):
                busy.append(symbol)
                continue
            try:
                # 拿到占位后**重读一次**最新状态 (别的线程可能刚写进去)
                current = trend_timing(data_dir, _heal=False)
                cur_as_of = current.get("as_of") if isinstance(current, dict) else None
                cur_behind = _behind_expected(cur_as_of)
                cur_intraday = _is_intraday_bar(cur_as_of)
                if not force and not cur_behind and not cur_intraday:
                    skipped[symbol] = 0.0
                    continue
                if not force and not _should_refetch(cur_as_of, symbol, force=force):
                    skipped[symbol] = round(time.time() - _last_fetch_at.get(symbol, 0.0), 1)
                    continue
                if time.time() >= deadline:
                    # 预算用完 → 剩下的标的这一轮不取, 下一轮再说
                    skipped[symbol] = 0.0
                    continue
                now = time.time()
                attempted.append(symbol)
                rows = fetch_tencent_daily(ts_symbol)
                # **记下尝试时刻** (成功与否都记) —— v0.3.44 只在写成功时记,
                # 于是「源站没出当天那根」时冷却表一直是空的 → 每个请求都去打接口,
                # 这正是 NAS 日志里每秒一条的刷屏。
                _last_fetch_at[symbol] = now
                if not rows:
                    fetch_failed = True
                    continue
                frame = to_kline_frame(rows, symbol)
                if frame.is_empty():
                    fetch_failed = True
                    continue
                # **只写本地缺的交易日** —— v0.3.44 是把腾讯返回的 320 根全写一遍,
                # 每个日分区一次读+merge+原子写, NAS 上单次约 4 分钟, 请求路径被拖死。
                local = _local_dates(data_dir, _TARGET_TABLE[kind], symbol)
                to_write = frame.filter(~pl.col("date").is_in(sorted(local))) if local else frame
                if to_write.is_empty():
                    # 没有新交易日 → 只有「当天那根还在跳」才值得重写一遍
                    latest = frame["date"].max()
                    cur_day = _parse_day(cur_as_of)
                    settle = _is_settle_refetch_needed(cur_as_of, symbol, now)
                    if cur_day is not None and latest == cur_day and (cur_intraday or settle):
                        to_write = frame.filter(pl.col("date") == latest)
                    else:
                        # 源站没有本地更新的东西 (腾讯当天那根还没出) → 本轮不写
                        continue
                else:
                    # 只补最近的一批 (有上限), 最新的优先 ——
                    # 全新安装时本地一张日K都没有, 一次写 320 个分区要 4 分钟;
                    # 限流后每轮补一批, 几轮就攒够 MA60 需要的根数。
                    limit = _MAX_WRITE_ROWS if max_rows is None else max(1, int(max_rows))
                    to_write = to_write.sort("date", descending=True).head(limit)
                if to_write.is_empty():
                    continue
                if kind == "etf":
                    repo.append_etf_daily(to_write)
                else:
                    repo.append_index_daily(to_write)
                fetched[symbol] = to_write.height
                # 写进去了 → as_of 前进 → 退避表清空
                _pending_backoff.pop(symbol, None)
                logger.info("free backfill %s via tencent: +%d rows", symbol, to_write.height)
            finally:
                _release_symbol(symbol)
        if fetched:
            try:
                repo.refresh_index_views()
            except Exception as e:  # noqa: BLE001
                logger.warning("refresh_index_views after backfill failed: %s", e)
            return {"ok": True, "fetched": fetched, "reason": "backfilled"}
        # 全部因冷却跳过 → 不是故障 (刚拉过, 数据就是它)。别报 ok=False,
        # 否则 `refresh_market_context` 会把它记成一条 error, 日志与 errors 里天天吵架。
        if skipped and len(skipped) == len(TIMING_TARGETS):
            return {"ok": True, "fetched": {}, "reason": "cooling", "cooldown": skipped}
        # 别的线程正在回补 → 也不是故障 (那一轮会把数据写进去)
        if busy:
            return {"ok": True, "fetched": {}, "reason": "busy", "inflight": busy}
        # 落后却没写进去 (源站还没发布当天那根) → 退避重试, 但**不算故障**
        if behind and not fetched and not fetch_failed:
            for symbol in attempted:
                cur = _pending_backoff.get(symbol, _PENDING_RETRY)
                _pending_backoff[symbol] = min(cur * 2, _PENDING_RETRY_MAX)
            logger.info(
                "timing index still behind (as_of=%s, %d days) → 源站尚未发布, 退避重试",
                as_of,
                stale,
            )
            return {
                "ok": True,
                "fetched": {},
                "reason": "source-lagging",
                "cooldown": skipped,
                "backoff": dict(_pending_backoff),
            }
        return {
            "ok": False,
            "fetched": {},
            "reason": "fetch-empty",
            "cooldown": skipped,
        }
    except Exception as e:  # noqa: BLE001 — 兜底链永不抛
        logger.warning("ensure_timing_index_fresh failed: %s", e)
        return {"ok": False, "fetched": fetched, "reason": f"error: {e}"}
