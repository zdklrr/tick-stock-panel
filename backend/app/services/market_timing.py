"""大盘择时 (market timing)——沪深300 收盘价 vs MA20/MA60 判四态, 并合成可执行建议仓位。

职责边界: 本模块只做「读本地日K → 算均线 → 判四态 → 乘出建议仓位」。
不联网、不下单、不写盘, HTTP 暴露在 app/api/risk.py。

## 为什么单独做一套「四态」而不是复用五档情绪
五档情绪 (`regime_builder.STATE_LABELS`) 衡量的是「今天盘面赚不赚钱」——
涨停家数、封板率、连板高度, 是**日内情绪**; 四态衡量的是「指数此刻站在均线上还是
均线下」, 是**趋势位置**。两者正交: 缩量阴跌可以情绪「震荡」但趋势已跌破 MA60;
暴涨一日可以情绪「强势」但指数仍在 MA60 之下。所以两个维度并存, 相乘才是仓位。

## 四态口径 (对齐 zdk 选股策略软件 market_regime())
- `risk_on`:   收盘 > MA20 且 > MA60 → 趋势偏多, 可按选股结果正常建仓
- `weak`:      只站上 MA20 (跌破 MA60) → 刚站上短期均线, 轻仓试单 + 严设止损
- `weak`:      只站上 MA60 (跌破 MA20) → 跌破短期均线但趋势未坏, 半仓 + 降换手
                (与上一态同为 `weak`, 但 advice 不同 —— zdk 也是这么合的)
- `risk_off`:  同时跌破 MA20/MA60 → 系统性风险期, 建议空仓或仅留底仓
- `unknown`:   本地日K 缺失/不足 60 根 → 数据缺失

## 设计铁律
1. **绝不联网**。NAS 上东财/新浪不可用时会拖慢整页; 取不到就 `ok=False` 返回 unknown,
   前端显示「数据缺失」而不是让页面卡住。
2. **绝不抛异常**。任何一路取数失败都降级, 因为调用方是页面首屏。
3. **系数相乘不能让结果归零**。合成仓位夹在 [SYN_FLOOR, SYN_CAP], 保底 0.15 ——
   「一天 0 成交」比亏钱更难用 (zdk 的 risk_off 二元闸门踩过这个坑)。
4. **三路分列**。`parts` 里每一路都给出系数与是否缺失, 用户能看到是谁在压仓位;
   缺失的一路按中性 1.0 参与并在 `parts[x].missing` 标记, 不静默当成 0。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 四态词汇 ────────────────────────────────────────────
TREND_RISK_ON = "risk_on"
TREND_WEAK = "weak"
TREND_RISK_OFF = "risk_off"
TREND_UNKNOWN = "unknown"

TREND_STATES: tuple[str, ...] = (TREND_RISK_ON, TREND_WEAK, TREND_RISK_OFF, TREND_UNKNOWN)

TREND_LABELS: dict[str, str] = {
    TREND_RISK_ON: "趋势偏多",
    TREND_WEAK: "趋势偏弱",
    TREND_RISK_OFF: "空头趋势",
    TREND_UNKNOWN: "数据缺失",
}

#: 四态 → 仓位系数 (zdk autotrade.EXPOSURE 同口径: 1.0 / 0.65 / 0.35 / 0.5)
TREND_EXPOSURE: dict[str, float] = {
    TREND_RISK_ON: 1.0,
    TREND_WEAK: 0.65,
    TREND_RISK_OFF: 0.35,
    TREND_UNKNOWN: 0.5,
}

#: 均线周期 (短期 / 长期)
MA_SHORT = 20
MA_LONG = 60
#: 默认标的与本地可接受的替代代码 (先 ETF 后指数: 用户数据目录里往往只有指数)
DEFAULT_SYMBOL = "510300"
SYMBOL_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    # (存储表, 代码, 展示用代码)
    ("kline_etf_daily", "510300", "510300"),
    ("kline_etf_daily", "510300.SH", "510300"),
    ("kline_index_daily", "000300.SH", "000300.SH"),
    ("kline_index_daily", "000300", "000300.SH"),
)
#: 拉取多少根日K (MA60 需要至少 60 根, 留余量给除权/停牌缺口)
LOAD_DAYS = 120
#: 少于这么多根就无法算 MA60
MIN_BARS = MA_LONG
#: 日K 滞后超过这么多「自然日」就在前端标「数据滞后」(指数快照只覆盖 8 只核心指数,
#: 000300.SH 常常不在最新分区里, 必须让用户看到用的是哪一天的收盘)
STALE_DAYS = 4

#: 合成仓位夹取区间 —— 下限保底不归零 (见铁律 3)
SYN_FLOOR = 0.15
SYN_CAP = 1.20
#: 同步自愈时**每个标的单轮最多写几个日分区** (v0.3.45)。
#: 落后时通常只缺当天那一根 → 写 1 个分区就够了, 请求路径不会被拖死;
#: 全新安装缺几十天 → 后台补算线程按 `_MAX_WRITE_ROWS` 分批攒。
_SYNC_MAX_WRITE_ROWS = 2
#: 合成文案分档 (从高到低, 取第一个满足的)
SYN_TIERS: tuple[tuple[float, str], ...] = (
    (1.00, "可以满仓做多"),
    (0.70, "正常仓位"),
    (0.45, "半仓为宜"),
    (0.25, "轻仓试单"),
)

#: 三路来源的中文名 (parts 里用)
PART_LABELS: dict[str, str] = {
    "timing": "大盘择时",
    "phase": "情绪周期",
    "risk": "外围风险",
}


# ── 数值工具 ────────────────────────────────────────────
def _f(value) -> float | None:
    """宽松转 float: 非数值 / NaN / inf 一律 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def ma(closes: list[float], n: int) -> float | None:
    """最近 n 根的算术均线; 数量不足或含非有限值返回 None。"""
    if n <= 0 or len(closes) < n:
        return None
    window = [_f(c) for c in closes[-n:]]
    if any(c is None for c in window):
        return None
    # 用最近 n 根的收盘均价; 不做除权还原 (指数本身不复权, ETF 短期除权影响可忽略)
    return round(sum(window) / n, 4)  # type: ignore[arg-type]


def _pct_vs(close: float | None, ref: float | None) -> float | None:
    """收盘相对均线的偏离 (**百分数数值**: 1.23 表示 +1.23%)。"""
    c = _f(close)
    r = _f(ref)
    if c is None or r is None or r == 0:
        return None
    return round((c - r) / r * 100.0, 2)


def _cn_today() -> date:
    """当前北京日期 (取不到时退回本机日期, 只是展示/推算用)。"""
    try:
        from app.market_time import cn_today

        return cn_today()
    except Exception:  # noqa: BLE001 — 时间模块不可用时退回本机日期
        return datetime.now().date()


#: 收盘后多久认为当日日K 已经**定型** (交易所 15:00 收盘, 留 5 分钟给数据源落库)
_CLOSE_READY = dt_time(15, 5)

#: 竞价结束时刻 (09:25)。用户明确要求「开盘前、最迟 9:25 竞价结束就该是最新的」,
#: 所以从这一刻起就认为当日应该有 K 线 (集合竞价成交价即当日开盘价)。
_SESSION_OPEN = dt_time(9, 25)


def _expected_as_of(now: datetime | None = None) -> date:
    """此刻日K **应该**已经更新到哪一天 (按周几推算, 不联网)。

    - 工作日 09:25 (竞价结束) 之后 → 当天;
    - 工作日 09:25 之前 → 上一个工作日;
    - 周六/周日 → 上周五。

    v0.3.40: 引入本函数替代「自然日数 > 1」的自愈触发条件。原条件的致命缺陷是
    **盘后必然失效** —— 交易日 15:00 收盘后 as_of 仍停在昨日时自然日差恒为 1,
    永远达不到 `> 1`, 于是整个晚上都不回补、卡片整晚挂着「滞后 1 天」, 直到
    次日才自愈 (用户报「本地的大盘择时不是今天的数据」正是这个)。

    v0.3.41: 阈值由 `_CLOSE_READY`(15:05) 提前到 `_SESSION_OPEN`(09:25 竞价结束)。
    用户原话「这些数据应该在每天开盘前就取到, 基本上 9:25 竞价结束就该是最新,
    这是最迟」。v0.3.40 只认收盘后的当天, 于是**整个交易时段都不回补**, 用户
    开盘后看到的仍是昨天的择时 (「都开盘了, 交易时间了, 情绪周期和大盘择时还是
    昨天的」)。集合竞价结束即有当日开盘价, 免费源 (腾讯/东财) 盘中也会返回当天
    这根 (成交量只有部分), 所以盘中取数是可行的; 未定型K 的副作用 (MA20/MA60/
    四态随行情跳动) 由 `intraday` 标记透出, 界面提示「盘中·未定型」而不是让它
    冒充收盘数据。

    法定节假日按工作日处理 (会把数据标成滞后一天, 至多持续到下一个交易日):
    本模块铁律是绝不联网, 拿不到真实交易日历 —— 用一次网络探针换这一天的不精确
    不值得, 且回补请求有 30 分钟冷却, 不会持续打接口。
    """
    stamp = now if now is not None else _cn_now()
    today = stamp.date()
    weekday = today.weekday()
    if weekday >= 5:  # 周六/周日 → 上周五
        return today - timedelta(days=weekday - 4)
    if stamp.time() >= _SESSION_OPEN:  # v0.3.41: 盘中即认当天
        return today
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:  # 周一盘前 → 上周五
        prev -= timedelta(days=1)
    return prev


def _is_intraday_bar(as_of: str | None, now: datetime | None = None) -> bool:
    """as_of 是否是**当日未定型**的盘中K (交易时段内, 收盘定型前)。

    只用于给界面加「盘中·未定型」提示; 收盘(15:05)后或 as_of 是历史日一律 False。
    """
    if not as_of:
        return False
    stamp = now if now is not None else _cn_now()
    try:
        day = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return False
    if day != stamp.date() or stamp.weekday() >= 5:
        return False
    return _SESSION_OPEN <= stamp.time() < _CLOSE_READY


def _cn_now() -> datetime:
    """当前北京时间 (取不到时退回本机时间)。"""
    try:
        from app.market_time import cn_now

        return cn_now()
    except Exception:  # noqa: BLE001 — 时间模块不可用时退回本机时间
        return datetime.now()


def _is_behind_expected(as_of: str | None, now: datetime | None = None) -> bool:
    """as_of 是否落后于此刻应该有的交易日 (自愈触发条件)。"""
    if not as_of:
        return True
    try:
        day = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return True
    return day < _expected_as_of(now)


def _stale_days(as_of: str | None, now: datetime | None = None) -> int | None:
    """as_of 落后应有交易日的**交易日数**; 解析不了返回 None。

    v0.3.40 起改为交易日口径 (原先用自然日):
    - 周五 15:35 时 as_of=周五 → 0 (最新);
    - 周一 09:00 时 as_of=周五 → 0 天? 不 —— 应为 1 个交易日落后吗? 否:
      此刻应有数据是周五 (周一还没收盘), 所以 as_of=周五 就是最新, 返回 0;
    - 周二 15:35 时 as_of=周五 → 2 (周一、周二两个交易日都缺)。

    周末不再虚报「滞后 1 天」(原先周六看周五数据会显示滞后 1 天)。
    """
    if not as_of:
        return None
    try:
        day = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return None
    expected = _expected_as_of(now)
    if day >= expected:
        return 0
    # 数 (day, expected] 区间内的周日数 —— 即缺了几个交易日
    missing = 0
    cursor = day + timedelta(days=1)
    while cursor <= expected:
        if cursor.weekday() < 5:
            missing += 1
        cursor += timedelta(days=1)
    return missing


# ── 四态判定 ────────────────────────────────────────────
def classify_trend(close, ma20, ma60) -> str:
    """收盘价 vs MA20/MA60 → 四态 (口径对齐 zdk market_regime)。

    均线缺失时返回 unknown; 只有一条均线可用时按「另一条未知」处理:
    能确认的跌破优先 (风险优先), 无法确认站上则归 unknown。
    """
    c = _f(close)
    s = _f(ma20)
    long_ma = _f(ma60)
    if c is None:
        return TREND_UNKNOWN
    if s is None and long_ma is None:
        return TREND_UNKNOWN
    if s is None:
        # 只有 MA60: 能判断跌破 → risk_off, 站上但不知短均 → weak (不能当偏多)
        return TREND_RISK_OFF if c <= long_ma else TREND_WEAK  # type: ignore[operator]
    if long_ma is None:
        return TREND_WEAK if c > s else TREND_RISK_OFF
    above_s = c > s
    above_l = c > long_ma
    if above_s and above_l:
        return TREND_RISK_ON
    if not above_s and not above_l:
        return TREND_RISK_OFF
    return TREND_WEAK


def trend_advice(state: str, close=None, ma20=None, ma60=None) -> str:
    """四态 → 一句话操作建议 (口径照抄 zdk 的四条文案)。"""
    if state == TREND_RISK_ON:
        return "大盘在 MA20/MA60 之上, 趋势偏多, 可按选股结果正常建仓。"
    if state == TREND_RISK_OFF:
        return "同时跌破 MA20/MA60, 系统性风险期, 建议空仓或仅留底仓。"
    if state == TREND_WEAK:
        s = _f(ma20)
        long_ma = _f(ma60)
        c = _f(close)
        if s is not None and c is not None and c > s and long_ma is not None and c <= long_ma:
            return "刚站上 MA20 但仍在 MA60 之下, 可轻仓试单、严设止损。"
        if long_ma is not None and c is not None and c > long_ma and s is not None and c <= s:
            return "跌破 MA20 但仍在 MA60 之上, 建议半仓并降低换手。"
        return "趋势偏弱, 建议降低仓位、严设止损。"
    return "本地缺少沪深300 日K (或不足 60 根), 无法判断大盘趋势, 按中性仓位处理。"


# ── 读本地日K ───────────────────────────────────────────
def _scan_closes(table: str, code: str, data_dir: Path, days: int) -> tuple[list[float], str | None]:
    """从 data_dir/<table> 读 code 的最近 days 根收盘价 (升序)。

    返回 (closes, as_of_iso)。取不到返回 ([], None), **不碰网络**。
    """
    base = Path(data_dir) / table
    if not base.exists():
        return [], None
    try:
        import polars as pl

        from app.parquet import scan_daily_parquet

        lf = scan_daily_parquet((base / "**" / "*.parquet").as_posix())
        df = (
            lf.filter(pl.col("symbol") == code)
            .select(["date", "close"])
            .sort("date")
            .tail(days)
            .collect()
        )
    except Exception as e:  # noqa: BLE001 — 缺表/parquet 损坏都按无数据处理, 页面首屏不能炸
        logger.warning("market_timing 读取 %s/%s 日K失败 (按数据缺失处理): %s", table, code, e)
        return [], None
    if df.is_empty():
        return [], None
    closes: list[float] = []
    last_day = None
    for row in df.to_dicts():
        c = _f(row.get("close"))
        if c is None or c <= 0:
            continue
        closes.append(c)
        d = row.get("date")
        last_day = str(d) if d is not None else last_day
    return closes, last_day


def _load_closes(data_dir, symbol: str) -> tuple[list[float], str | None, str]:
    """按 SYMBOL_CANDIDATES 顺序尝试, 返回 (closes, as_of, 实际代码)。

    优先返回第一个达到 MIN_BARS 的候选; 都不够时返回**根数最多的那个**,
    让调用方能把真实的 `bars` 报给前端 (「只有 30 根, 不够算 MA60」比「0 根」有用)。
    """
    candidates = list(SYMBOL_CANDIDATES)
    if symbol and symbol not in {c for _, c, _ in candidates}:
        # 调用方显式指定了别的代码: 优先试它 (ETF 表 + 指数表各一次)
        candidates = [
            ("kline_etf_daily", symbol, symbol),
            ("kline_index_daily", symbol, symbol),
            *candidates,
        ]
    best: tuple[list[float], str | None, str] = ([], None, symbol or DEFAULT_SYMBOL)
    for table, code, shown in candidates:
        closes, as_of = _scan_closes(table, code, data_dir, LOAD_DAYS)
        if len(closes) >= MIN_BARS:
            return closes, as_of, shown
        if len(closes) > len(best[0]):
            best = (closes, as_of, shown)
    return best


def _is_healable_dir(data_dir) -> bool:
    """这个 data_dir 是否允许联网回补 + 写盘 (v0.3.39)。

    允许自愈的必须**同时**满足:
    - 是正式数据目录 (`data_dir is None` 走 settings, 或解析后与 settings.data_dir 同路径);
    - 不在系统临时目录下 (pytest 的 tmp_path 会把 settings.data_dir 也 monkeypatch
      成临时目录, 只比对 settings 会让单测意外联网回补并污染断言)。

    其他情况一律只读不写。原先的判定是「调用方有没有省掉 data_dir」, 但 HTTP 层
    一律显式传 `repo.store.data_dir`, 导致生产路径上自愈成了死代码 (桌面端大盘择时
    长期停在旧日期)。改为按**路径身份**判定。
    """
    try:
        import tempfile
        from pathlib import Path

        from app.config import settings

        target = Path(settings.data_dir if data_dir is None else data_dir).resolve()
        if data_dir is not None and target != Path(settings.data_dir).resolve():
            return False
        # 临时目录 (含 pytest tmp_path) 一律不自愈
        tmp_root = Path(tempfile.gettempdir()).resolve()
        return not (target == tmp_root or tmp_root in target.parents)
    except Exception as e:  # noqa: BLE001 — 配置/路径异常时保守不自愈
        logger.warning("market_timing 判定自愈目录失败 (按只读处理): %s", e)
        return False


# ── 大盘择时快照 ────────────────────────────────────────
def _build_snapshot(data_dir, symbol: str, days: int) -> dict:
    """只读计算快照 (绝不联网、绝不抛); 自愈由 `trend_timing` 在外层负责。"""
    payload: dict = {
        "ok": False,
        "symbol": symbol or DEFAULT_SYMBOL,
        "as_of": None,
        "close": None,
        "ma20": None,
        "ma60": None,
        "pct_vs_ma20": None,
        "pct_vs_ma60": None,
        "state": TREND_UNKNOWN,
        "state_label": TREND_LABELS[TREND_UNKNOWN],
        "exposure": TREND_EXPOSURE[TREND_UNKNOWN],
        "bars": 0,
        "stale_days": None,
        "stale": False,
        "intraday": False,
        "advice": trend_advice(TREND_UNKNOWN),
    }
    if data_dir is None or not Path(data_dir).exists():
        return payload

    try:
        closes, as_of, used = _load_closes(data_dir, symbol or DEFAULT_SYMBOL)
        payload["symbol"] = used
        payload["bars"] = len(closes)
        window = closes[-max(int(days), MIN_BARS):] if closes else []
        if len(window) < MIN_BARS:
            payload["advice"] = trend_advice(TREND_UNKNOWN)
            return payload
        close = round(window[-1], 4)
        ma20 = ma(window, MA_SHORT)
        ma60 = ma(window, MA_LONG)
        state = classify_trend(close, ma20, ma60)
        age = _stale_days(as_of)
        payload.update({
            "ok": state != TREND_UNKNOWN,
            "as_of": as_of,
            "close": close,
            "ma20": ma20,
            "ma60": ma60,
            "pct_vs_ma20": _pct_vs(close, ma20),
            "pct_vs_ma60": _pct_vs(close, ma60),
            "state": state,
            "state_label": TREND_LABELS[state],
            "exposure": TREND_EXPOSURE[state],
            "stale_days": age,
            "stale": bool(age is not None and age > STALE_DAYS),
            "intraday": _is_intraday_bar(as_of),
            "advice": trend_advice(state, close, ma20, ma60),
        })
    except Exception as e:  # noqa: BLE001 — 任何意外都降级, 不能让首屏崩
        logger.warning("market_timing 计算失败 (按数据缺失处理): %s", e)
        payload["state"] = TREND_UNKNOWN
        payload["state_label"] = TREND_LABELS[TREND_UNKNOWN]
        payload["exposure"] = TREND_EXPOSURE[TREND_UNKNOWN]
        payload["advice"] = trend_advice(TREND_UNKNOWN)
    return payload


def trend_timing(data_dir=None, *, symbol: str = DEFAULT_SYMBOL, days: int = LOAD_DAYS, _heal: bool = True) -> dict:
    """大盘择时快照: 沪深300 收盘 vs MA20/MA60 → 四态 + 系数 + 建议。

    Args:
        data_dir: 数据根目录; None 时用 app.config.settings.data_dir。
        symbol: 优先尝试的标的代码 (默认 510300, 缺失时自动退 000300.SH)。
        days: 需要多少根日K (默认 120, 必须 >= 60 才能算 MA60)。

    计算本身**只读本地**; 数据落后于应有交易日时, 外层会调免费源回补一次再重读
    (见下方 v0.3.35/39/40 注释)。取不到时返回 ok=False/unknown, 绝不抛。
    `pct_vs_ma20` / `pct_vs_ma60` 是**百分数数值** (-1.23 = -1.23%)。
    """
    if data_dir is None:
        try:
            from app.config import settings

            data_dir = settings.data_dir
        except Exception as e:  # noqa: BLE001 — 配置不可用也要返回降级结构
            logger.warning("market_timing 读取配置失败 (按数据缺失处理): %s", e)
            data_dir = None

    payload = _build_snapshot(data_dir, symbol, days)

    # v0.3.35: 数据滞后 (如 tickflow key 过期导致 sync_index 被 skip) 时,
    # 用免费源 (腾讯) 兜底回补 000300/510300 日K后**重读一次** —— 软失败不抛,
    # 回补不了仍返回原结果 (首屏永远优先可用性)。冷却 30 分钟, 不会频繁打接口。
    #
    # v0.3.39: 门控从「调用方是否省了 data_dir」改为「data_dir 是否就是正式数据目录」。
    # 原实现只在 data_dir is None 时放行, 但 HTTP 层 (api/risk.py) 一律显式传
    # `request.app.state.repo.store.data_dir` —— 于是**生产路径上自愈永远不会触发**,
    # 装好的桌面端大盘择时就一直停在旧日期 (只有手动脚本 force=True 能救)。
    # 现在按「与 settings.data_dir 是否同一目录」判定: 测试用 tmp_path 仍只读不写,
    # 正式运行无论调用方传不传都能自愈。
    #
    # v0.3.40: ①触发条件从「自然日 > 1」改为「落后于此刻应有的交易日」—— 原条件在
    #   盘后必然失效 (自然日差恒为 1), 整晚不回补; ②计算与自愈拆开 (`_build_snapshot`),
    #   使得「本地一根日K都没有」「根数不足 MA60」这类**提前返回**的分支也能走到自愈
    #   (原先它们 return 在自愈之前, 全新安装永远补不上数据)。
    # v0.3.41: 当日那根K 在盘中还没定型, 也要刷新; 但**不能同步等网络** ——
    #   页面上四张卡会同时读这个接口, 同步拉腾讯会让首屏卡 20 秒以上 (NAS 实测
    #   `/api/risk/timing` 直接超时)。所以分两种:
    #   · 数据缺失/落后 → 同步回补 (没有数据就没什么可显示的, 值得等);
    #   · 只是盘中未定型 → 起后台线程去刷, 立刻返回当前快照, 下次轮询自然读到新值。
    #
    # v0.3.45: 同步回补**必须是有界的**。v0.3.44 的同步路径把腾讯返回的 320 根
    #   全部 merge-upsert (NAS 一次约 4 分钟) → `/api/risk/timing` 与
    #   `/api/risk/advice` 直接 120 秒超时。现在同步路径每个标的最多写
    #   `_SYNC_MAX_WRITE_ROWS` 个日分区 (通常只缺当天那一根 → 1 个分区),
    #   剩余缺口交给后台补算线程慢慢攒。
    _allow_heal = _heal and _is_healable_dir(data_dir)
    if _allow_heal:
        _missing = not payload.get("ok") or _is_behind_expected(payload.get("as_of"))
        if _missing:
            try:
                from app.services.index_daily_free import ensure_timing_index_fresh

                healed = ensure_timing_index_fresh(data_dir, max_rows=_SYNC_MAX_WRITE_ROWS)
                if healed.get("fetched"):
                    retry = _build_snapshot(data_dir, symbol, days)
                    # 回补有效 (能判四态, 或至少根数变多) 才替代原结果
                    if retry.get("ok") or retry.get("bars", 0) > payload.get("bars", 0):
                        retry["backfilled"] = True
                        return retry
            except Exception as e:  # noqa: BLE001 — 兜底链自身永不影响主路径
                logger.warning("market_timing 免费源兜底失败: %s", e)
            # 同步只补了最近的一小截 → 后台把剩下的交易日补齐 (不等它)
            _kick_intraday_refresh(data_dir)
        elif _is_intraday_bar(payload.get("as_of")):
            _kick_intraday_refresh(data_dir)
    return payload


#: 同一时刻只放一个盘中刷新线程, 并记下最近一次放行时间 (与
#: `index_daily_free._INTRADAY_COOLDOWN` 同数量级即可, 真正节流在那边)。
_intraday_thread: threading.Thread | None = None
_intraday_kicked_at: float = 0.0
_INTRADAY_KICK_PERIOD = 60.0


def _kick_intraday_refresh(data_dir) -> None:
    """起一个**后台**线程刷新盘中K, 不等结果 (页面首屏不能卡在网络)。

    失败只打日志; 线程 daemon 化, 不会拖住进程退出。
    """
    global _intraday_thread, _intraday_kicked_at
    now = time.time()
    if now - _intraday_kicked_at < _INTRADAY_KICK_PERIOD:
        return
    if _intraday_thread is not None and _intraday_thread.is_alive():
        return
    _intraday_kicked_at = now

    def _run() -> None:
        try:
            from app.services.index_daily_free import ensure_timing_index_fresh

            result = ensure_timing_index_fresh(data_dir)
            if result.get("fetched"):
                logger.info("盘中K 后台刷新: %s", result.get("fetched"))
        except Exception as e:  # noqa: BLE001 — 后台刷新失败不该惊动任何人
            logger.warning("盘中K 后台刷新失败: %s", e)

    _intraday_thread = threading.Thread(target=_run, name="timing-intraday-refresh", daemon=True)
    _intraday_thread.start()


# ── 情绪周期系数 ────────────────────────────────────────
def _phase_part(data_dir: Path) -> dict:
    """情绪周期 → 仓位系数 (走 trade_plan.exposure_multiplier, 与模拟盘同源)。

    刻意复用 trade_plan 而不是自己查六阶段表: 交易计划下单时乘的就是这个函数,
    卡片上显示的数字必须与实盘生效的数字同源, 否则用户会看到两个不一致的系数。
    """
    part = {
        "key": "phase",
        "label": PART_LABELS["phase"],
        "value": 1.0,
        "missing": True,
        "state": None,
        "state_label": None,
        "detail": "数据缺失 (按 1.0 计)",
    }
    try:
        from app.strategy import trade_plan

        state = trade_plan.latest_state(data_dir)
        if not state:
            return part
        value = _f(trade_plan.exposure_multiplier(state))
        if value is None:
            return part
        part.update({
            "value": round(value, 3),
            "missing": False,
            "state": state.get("state"),
            "state_label": state.get("label"),
            "detail": f"{state.get('label') or state.get('state')} "
                      f"({state.get('date') or '无日期'})",
        })
    except Exception as e:  # noqa: BLE001 — 情绪模块缺失按中性参与, 不阻断合成
        logger.warning("market_timing 读取大盘情绪失败 (按 1.0 计): %s", e)
    return part


# ── 外围风险系数 ────────────────────────────────────────
def _risk_part(data_dir: Path, account_id: str) -> dict:
    """风险雷达 → 仓位系数 (取 global_exposure, 不含账户回撤那一路)。

    刻意用 `global_exposure` 而不是合成 `exposure`: 后者已经乘过账户回撤,
    而回撤是「账户自身盈亏」不是「大盘风险」, 放到这里的合成值里会重复计一次。
    """
    part = {
        "key": "risk",
        "label": PART_LABELS["risk"],
        "value": 1.0,
        "missing": True,
        "state": None,
        "state_label": None,
        "detail": "数据缺失 (按 1.0 计)",
    }
    try:
        from app.services import global_risk

        snap = global_risk.radar_snapshot(data_dir, account_id=account_id)
        if not snap.get("ok"):
            return part
        value = _f(snap.get("global_exposure"))
        if value is None:
            return part
        part.update({
            "value": round(value, 3),
            "missing": False,
            "state": snap.get("level"),
            "state_label": snap.get("level_text"),
            "detail": f"风险分 {_f(snap.get('score')) or 0:.1f} ({snap.get('level_text') or '未知'})",
        })
    except Exception as e:  # noqa: BLE001 — 雷达不可用按中性参与
        logger.warning("market_timing 读取风险雷达失败 (按 1.0 计): %s", e)
    return part


# ── 合成 ────────────────────────────────────────────────
def synth_label(value: float) -> str:
    """合成仓位 → 中文档位 (>=1.0 满仓 ... <0.25 空仓)。"""
    v = _f(value)
    if v is None:
        return "数据缺失"
    for threshold, text in SYN_TIERS:
        if v >= threshold:
            return text
    return "建议空仓或仅留底仓"


def _synth_advice(value: float, label: str, parts: dict) -> str:
    """一句话说明「因为 X 所以 Y」: 列出压制最狠的那一路。"""
    drivers = [
        p for p in parts.values()
        if isinstance(p, dict) and not p.get("missing") and _f(p.get("value")) is not None
    ]
    missing = [p.get("label") for p in parts.values() if isinstance(p, dict) and p.get("missing")]
    if not drivers:
        return "三路数据都缺失, 建议按中性仓位 (50%) 执行; 补齐数据后再看这张卡。"
    worst = min(drivers, key=lambda p: _f(p.get("value")) or 1.0)
    head = f"建议仓位 {value * 100:.0f}% ({label})。"
    why = (
        f"主要压制来自{worst.get('label')} ×{_f(worst.get('value')) or 1.0:.2f}"
        f" ({worst.get('state_label') or '状态未知'})"
    )
    rest = [
        f"{p.get('label')} ×{_f(p.get('value')) or 1.0:.2f}"
        for p in drivers if p is not worst
    ]
    tail = f"; 另 {', '.join(rest)}。" if rest else "。"
    # 极弱态才给「空仓 / 仅留底仓」, 不做二元闸门 (见铁律 3)
    if value < 0.25:
        tail += " 已经很低, 建议空仓或仅留底仓观察, 不必完全退出。"
    elif value < 0.45:
        tail += " 可轻仓试单, 止损收紧。"
    if missing:
        tail += f" {', '.join(missing)}数据缺失, 已按 1.0 中性计。"
    return head + why + tail


def _paper_part(data_dir: Path, account_id: str) -> dict:
    """模拟盘当前实际生效的系数 (计划列表里的 last_adj / timing_scale)。"""
    out = {
        "active_plans": 0,
        "last_adj": None,
        "last_adj_reason": None,
        "timing_scale": None,
        "note": "该建议将在你新建交易计划时生效。",
    }
    try:
        from app.strategy import paper_plans

        plans = paper_plans.list_plans(data_dir, account_id, status=("pending", "bought", "holding"))
        out["active_plans"] = len(plans)
        adj = [_f(p.get("last_adj")) for p in plans]
        adj = [v for v in adj if v is not None]
        scales = [_f(p.get("timing_scale")) for p in plans]
        scales = [v for v in scales if v is not None]
        if plans:
            out["note"] = (
                f"当前有 {len(plans)} 条活跃计划; 系数在下单时刻乘到买入股数上, 卖出永不受影响。"
            )
        if adj:
            out["last_adj"] = round(sum(adj) / len(adj), 3)
        if scales:
            out["timing_scale"] = round(sum(scales) / len(scales), 3)
        reasons = [p.get("last_adj_reason") for p in plans if p.get("last_adj_reason")]
        if reasons:
            out["last_adj_reason"] = reasons[0]
    except Exception as e:  # noqa: BLE001 — 计划模块异常不影响建议仓位
        logger.warning("market_timing 读取模拟盘计划失败: %s", e)
    return out


def synthesize(data_dir=None, *, account_id: str = "default") -> dict:
    """三路系数相乘 → 建议仓位 (可执行数字)。

    timing (大盘四态) × phase (情绪周期) × risk (外围风险), 夹到 [SYN_FLOOR, SYN_CAP]。
    任何一路取数失败按中性 1.0 参与并在 `parts[x].missing` 标记, **绝不抛异常**。

    返回:
        {ok, suggested_exposure, suggested_exposure_pct, label, advice,
         parts: {timing, phase, risk}, detail: {raw, clamped, paper}}
    """
    if data_dir is None:
        try:
            from app.config import settings

            data_dir = settings.data_dir
        except Exception as e:  # noqa: BLE001
            logger.warning("market_timing 合成时读取配置失败: %s", e)
            data_dir = None
    d = Path(data_dir) if data_dir is not None else Path(".")

    timing = trend_timing(d)
    t_part = {
        "key": "timing",
        "label": PART_LABELS["timing"],
        "value": _f(timing.get("exposure")) or TREND_EXPOSURE[TREND_UNKNOWN],
        "missing": timing.get("state") == TREND_UNKNOWN,
        "state": timing.get("state"),
        "state_label": timing.get("state_label"),
        "detail": (
            f"收盘 {timing.get('close')} / MA20 {timing.get('ma20')} / MA60 {timing.get('ma60')}"
            if timing.get("ok") else "数据缺失 (按 1.0 计)"
        ),
    }
    if t_part["missing"]:
        # 缺失时按中性 1.0 参与, 而不是用 unknown 的 0.5 硬压仓位
        t_part["value"] = 1.0
        t_part["detail"] = "数据缺失 (按 1.0 计)"

    phase = _phase_part(d)
    risk = _risk_part(d, account_id)
    parts = {"timing": t_part, "phase": phase, "risk": risk}

    raw = 1.0
    for p in parts.values():
        v = _f(p.get("value"))
        raw *= v if v is not None else 1.0
    value = _clamp(raw, SYN_FLOOR, SYN_CAP)
    label = synth_label(value)
    return {
        "ok": True,
        "suggested_exposure": round(value, 3),
        "suggested_exposure_pct": round(value * 100, 1),
        "label": label,
        "advice": _synth_advice(value, label, parts),
        "parts": parts,
        "detail": {
            "raw": round(raw, 4),
            "clamped": round(raw, 4) != round(value, 4),
            "floor": SYN_FLOOR,
            "cap": SYN_CAP,
            "paper": _paper_part(d, account_id),
            "timing": timing,
        },
    }


__all__ = [
    "DEFAULT_SYMBOL",
    "MA_LONG",
    "MA_SHORT",
    "MIN_BARS",
    "PART_LABELS",
    "STALE_DAYS",
    "SYMBOL_CANDIDATES",
    "SYN_CAP",
    "SYN_FLOOR",
    "SYN_TIERS",
    "TREND_EXPOSURE",
    "TREND_LABELS",
    "TREND_STATES",
    "classify_trend",
    "ma",
    "synth_label",
    "synthesize",
    "trend_advice",
    "trend_timing",
]
