"""A股市场时间工具 — 固定北京时间 (UTC+8, 无夏令时)。

服务器/容器本地时区不可靠 (python:slim 镜像默认 UTC), 交易时段判断、
实时行情落盘日期等必须显式使用北京时间, 否则 Docker 部署时轮询窗口
与真实交易时段完全错开 (北京 9:15-15:05 = UTC 1:15-7:05)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

CN_TZ = timezone(timedelta(hours=8))

# A 股交易时段 (北京时间): 上午 9:30-11:30 (120 分钟) + 下午 13:00-15:00 (120 分钟) = 240 分钟
_TRADING_TOTAL_MINUTES = 240
_MORNING_START = dt_time(9, 30)
_MORNING_END = dt_time(11, 30)
_AFTERNOON_START = dt_time(13, 0)
_AFTERNOON_END = dt_time(15, 0)


def cn_now() -> datetime:
    """当前北京时间 (带时区)。"""
    return datetime.now(CN_TZ)


def cn_today() -> date:
    """当前北京日期。"""
    return datetime.now(CN_TZ).date()


def in_continuous_session(now: datetime | None = None) -> bool:
    """A股连续竞价时段 (北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。"""
    now = now or cn_now()
    return now.weekday() < 5 and (
        _MORNING_START <= now.time() <= _MORNING_END
        or _AFTERNOON_START <= now.time() <= _AFTERNOON_END
    )


#: 「当日数据还会在变」的窗口 (北京时间): 集合竞价 9:15 开始 → 收盘定型 15:05。
#: 这个窗口里日K/情绪周期都还没定型, 需要高频复查; 窗口外按天级节奏就够了。
_AUCTION_START = dt_time(9, 15)
_CLOSE_SETTLED = dt_time(15, 5)


def in_data_refresh_window(now: datetime | None = None) -> bool:
    """当日行情/日K 还在变的时段 (工作日 9:15-15:05), 用于决定刷新频率。

    背景 (v0.3.45): 用户在竞价结束 (09:25) 就打开页面, 情绪周期与大盘择时
    还停在昨天 —— 后台补算原本 30 分钟才跑一次, 盘中更是要等半小时。
    窗口内按分钟级复查, 窗口外回到 30 分钟一次。
    """
    now = now or cn_now()
    return now.weekday() < 5 and _AUCTION_START <= now.time() <= _CLOSE_SETTLED


#: 窗口内/外的复查间隔 (秒) —— 见 `data_refresh_interval_seconds`。
_REFRESH_INTERVAL_ACTIVE = 180
_REFRESH_INTERVAL_IDLE = 1800


def data_refresh_interval_seconds(now: datetime | None = None) -> int:
    """后台「择时/情绪周期」补算的复查间隔 (秒)。

    当日数据还在变 (竞价 09:15 - 收盘定型 15:05) → 3 分钟一次;
    其余时段 → 30 分钟一次 (日K 一天才定型一次, 不用更勤)。
    """
    return _REFRESH_INTERVAL_ACTIVE if in_data_refresh_window(now) else _REFRESH_INTERVAL_IDLE


def trading_minutes_elapsed_from_dt(dt: datetime) -> float:
    """根据北京时间 datetime 计算当日已交易分钟数。

    交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
    - 开盘前 = 0; 午休(11:30-13:00) = 120(保持上午累计); 收盘后 = 240。
    - 非交易日(周末) = 240 (视作全天, 避免量比被折算成 0)。
    """
    if dt.weekday() >= 5:
        return float(_TRADING_TOTAL_MINUTES)
    t = dt.time()
    if t < _MORNING_START:
        return 0.0
    if t < _MORNING_END:
        return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
    if t < _AFTERNOON_START:
        return 120.0  # 午休, 保持上午累计
    if t < _AFTERNOON_END:
        return 120.0 + (dt.hour * 60 + dt.minute - 13 * 60) + dt.second / 60.0
    return float(_TRADING_TOTAL_MINUTES)


def trading_minutes_elapsed() -> float:
    """当前已交易分钟数 (基于服务端北京时间)。

    量比折算的兜底: 当行情 timestamp 缺失时用服务端时间。
    优先使用 trading_minutes_elapsed_from_ts (行情真实时间, 更准)。
    """
    return trading_minutes_elapsed_from_dt(cn_now())


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。

    优先使用此函数: 行情 timestamp 是真实成交时间, 比服务端时间更准
    (服务端时间含网络/限流延迟)。

    Args:
        ts_ms: 毫秒级 Unix 时间戳 (TickFlow SDK quote.timestamp / kline.timestamp)

    Returns:
        已交易分钟数 (0~240)。timestamp 为 None/无效时返回 240 (视作全天,
        避免量比被折算成 0)。
    """
    if not ts_ms:
        return float(_TRADING_TOTAL_MINUTES)
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=CN_TZ)
    except (ValueError, TypeError, OSError):
        return float(_TRADING_TOTAL_MINUTES)
    return trading_minutes_elapsed_from_dt(dt)

