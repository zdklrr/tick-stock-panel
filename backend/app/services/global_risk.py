"""风险雷达 (global risk)——外围股指 + A 股宽度 + 账户回撤, 合成一个 0~100 的风险分。

职责边界: 本模块只做「取数 + 打分 + 映射仓位系数」, 不碰订单、不落盘业务数据
(只在 data_dir/cache 下写一个短 TTL 的响应缓存)。HTTP 暴露在 app/api/risk.py。

## 设计铁律 (与 zdk 选股策略软件的 globalrisk.py 一致, 都是有代价换来的)

1. 外围不是 A 股的领先指标, 只是风险温度的旁证。标普日收益与 A 股日收益的
   相关系数只有 0.2~0.3, 只有极端行情 (美股单日跌 3% 以上、波动率骤升) 才真的
   传导。所以这里的权重是「温和缩放」, 不是「美股跌了就空仓」。

2. 日经 / KOSPI 比 A 股早 1.5 小时开盘, 属于「同步」不是「领先」, 只能当当日
   风险偏好的确认; 恒生与 A 股同时开盘。区域权重因此是 us 0.45 > asia 0.35 >
   hk 0.20: 美股是 A 股开盘前的定价锚, 亚太是同时段情绪, 恒生权重最低。

3. 回撤熔断是减速带不是刹车 —— 全部是系数且带下限, 永远不会把仓位算成 0。
   曾经写成二元闸门 (回撤超阈值就不买), 结果是「一天 0 成交」, 比亏钱更难用。

4. 所有风险源统一归一到 0~100 分 (50 = 中性, 越高越危险), 再由同一个函数
   `score_to_exposure()` 映射成仓位系数, 三个来源用同一把尺子, 可比可解释。

5. 缺数据就说缺数据。任一指数取不到就进 `missing` 列表并在前端灰显,
   绝不编造 (实测新浪 `int_*` 系列数值已过期, 差 10% 以上, 绝对不能用)。

## 数据源 (全部实测可用, 2026-11-03)
- 美股现价+涨跌幅: 新浪 `hq.sinajs.cn/list=gb_$dji,gb_$ixic,gb_$inx` (GB18030)。
- 亚太/恒生现价: 新浪 `znb_NKY,znb_KOSPI,znb_TWSE,znb_HSI`, 带日期字段可校验新鲜度。
- 快照兜底: 东财 `push2.eastmoney.com/api/qt/ulist.np/get` 批量拿 7 个指数的现价与
  涨跌幅。新浪的台湾 TWSE 源停更一年多、恒生涨幅恒 0.00, 这两处必须靠东财补。
- 20 日均线 (MA20 偏离): 东财 `push2his.eastmoney.com/api/qt/stock/kline/get` 单只拉取。
  东财会按 IP 间歇封禁 (返回 `{"rc":102,"data":null}`), 所以排在最后、失败静默。
- 美股日线备选: 新浪 `US_MinKService.getDailyK` (5747 根全历史, 实测可用)。
- 富时中国 A50 期货: 新浪 `hf_CHA50CFD`。离岸人民币: 新浪 `fx_susdcnh`。

## 坑 (逐条踩过)
- **新浪 `znb_*` 第 5 列的 unix 时间戳与第 6 列的日期字符串互相矛盾**
  (实测 `znb_NKY`: 时间戳 1759126320 = 2025-09-29, 而日期字符串 = 2026-11-02,
  真实行情日期是后者)。所以 `_parse_sina_znb` 必须**先解析看得见的日期字符串**,
  unix 时间戳只能兜底。反过来会把亚太三个指数全部误判为「过期一年多」→
  `missing=[N225,KS11,TWII]`、风险分从 69 掉到 62 —— 静默错数。
- 台湾 `znb_TWSE` 的新浪源已一年多没更新 (实测停在 2025-09-26), 必须按日期校验,
  超期丢弃并改用东财。`_parse_sina_znb` 里做了 STALE_DAYS 判断。
- 恒生 `znb_HSI` 只有 6 列且涨跌幅恒 0.00 (源的问题), 靠东财快照补真实涨幅。
- 腾讯标普代码是 `usINX` 不是 `usSPX`; 腾讯没有日经/KOSPI (`jpNI225` 返回空)。
- Yahoo 是韩台备用通道但会 403/429, 需要退避; 本模块暂不接 Yahoo, 避免半可用通道
  让用户以为「有数据」。
- `meta.chartPreviousClose` 是区间起点前收盘, 不是昨收 —— 算涨跌幅不能用它。
"""
from __future__ import annotations

import json as _json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────

_SINA_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}

_EM_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

_SINA_HQ = "https://hq.sinajs.cn/list="
_EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EM_QUOTE = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_SINA_US_DAILY = "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
#: 新浪全球期货日线 (JSONP)。不是股指现货, 但同一标的的「相对 20 日均线偏离」
#: 与现货几乎一致, 所以在东财被墙/封禁时用它算 MA20 是合理近似 (见 _sina_futures_daily)。
_SINA_GLOBAL_FUT = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_x=/GlobalFuturesService.getGlobalFuturesDailyKLine"
)

#: 数据过期阈值 (自然日)。超过这个天数没更新就标 stale 且不计入分数。
STALE_DAYS = 4

#: 响应缓存 TTL 秒。前端 60s 轮询, 不能每次都联网打 8 个请求。
CACHE_TTL = 45.0

#: 单个指数涨幅在打分前先夹 ±3%, 避免极端值把区域分拉爆。
CHG_CAP = 3.0
#: 涨幅 → 分数的斜率 (1% 涨跌 = 20 分)。
CHG_K = 20.0
#: MA20 偏离 → 分数的斜率, 再夹 ±MA_CAP 分。
MA_K = 1.5
MA_CAP = 10.0

#: 风险分 → 仓位系数, 夹在 [FLOOR, CAP]。
#: 30 分 → 1.10, 50 → 1.00, 60 → 0.87, 70 → 0.74, 85 → 0.55, 100 → 0.35。
#: 下限是硬约束: 风险再高也不空仓 (见铁律 3)。
EXPOSURE_FLOOR = 0.35
EXPOSURE_CAP = 1.10

#: 账户回撤阶梯 (回撤%, 系数)。回撤越深系数越小, 但最低 DD_FLOOR_FACTOR。
DD_STEPS: tuple[tuple[float, float], ...] = (
    (5.0, 1.00),
    (10.0, 0.80),
    (15.0, 0.60),
    (20.0, 0.40),
)
DD_FLOOR_FACTOR = 0.20
#: 回撤窗口 (交易日)。用「窗口内当前回撤」(峰值→现在), 刻意不用 max_dd,
#: 否则一旦深亏过就永久缩仓, 永远恢复不了。
DD_WINDOW_DAYS = 60

#: 环球指数定义。weight 是区域内权重, region 用于区域合成。
#: sina_fut 是「用同标的新浪全球期货日线算 MA20」的备用通道, 只在东财日线与
#: 新浪美股日线都拿不到时启用 (群晖 NAS 容器里东财整站被阻断就是这个场景)。
GLOBAL_INDEXES: tuple[dict, ...] = (
    # —— 美股: A 股开盘前的定价锚 ——
    {"code": "DJIA", "name": "道琼斯", "region": "us", "weight": 0.20,
     "sina_gb": "gb_$dji", "em_secid": "100.DJIA", "sina_us": ".DJI", "sina_fut": "YM"},
    {"code": "IXIC", "name": "纳斯达克", "region": "us", "weight": 0.50,
     "sina_gb": "gb_$ixic", "em_secid": "100.NDX", "sina_us": ".IXIC", "sina_fut": "NQ"},
    {"code": "SPX", "name": "标普500", "region": "us", "weight": 0.30,
     "sina_gb": "gb_$inx", "em_secid": "100.SPX", "sina_us": ".INX", "sina_fut": "ES"},
    # —— 亚太: 比 A 股早开盘 1.5 小时, 属「同步」确认 ——
    {"code": "N225", "name": "日经225", "region": "asia", "weight": 0.40,
     "sina_znb": "znb_NKY", "em_secid": "100.N225", "sina_fut": "NK"},
    {"code": "KS11", "name": "韩国KOSPI", "region": "asia", "weight": 0.40,
     "sina_znb": "znb_KOSPI", "em_secid": "100.KS11"},
    {"code": "TWII", "name": "台湾加权", "region": "asia", "weight": 0.20,
     "sina_znb": "znb_TWSE", "em_secid": "100.TWII"},
    # —— 恒生: 与 A 股同时开盘, 同步非领先, 权重最低 ——
    {"code": "HSI", "name": "恒生指数", "region": "hk", "weight": 1.00,
     "sina_znb": "znb_HSI", "em_secid": "100.HSI", "sina_fut": "HSI"},
)

#: 区域权重 (us 是定价锚, asia 是同时段情绪, hk 同步非领先)。
REGION_WEIGHT: dict[str, float] = {"us": 0.45, "asia": 0.35, "hk": 0.20}

#: 风险等级 (分数越高越危险)。
LEVEL_TEXT = {"low": "低", "normal": "正常", "elevated": "偏高", "high": "高", "unknown": "未知"}

# 进程内缓存: {key: (ts, payload)}。key 含 data_dir 与账户, 避免串号。
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


# ── 小工具 ──────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _f(value) -> float | None:
    """宽松转 float: 非数值 / NaN / inf 一律 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_date_explicit(text) -> date | None:
    """只解析「看得见的日期字符串」(2026-11-02 / 9/26/2025 / 2026-11-03 05:51:02)。

    刻意不解析 unix 时间戳: 实测新浪 znb_* 的第 5 列 unix 秒与第 6 列的日期字符串
    互相矛盾 (第 5 列 1759126320 = 2025-09-29, 第 6 列 = 2026-11-02, 真实行情日期
    是后者)。日期字符串优先, 时间戳只能作兜底。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or s.replace(".", "").isdigit():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_date_timestamp(text) -> date | None:
    """把 unix 秒时间戳解析成 date (只在没有日期字符串时兜底)。"""
    if text is None:
        return None
    try:
        n = float(str(text).strip())
    except ValueError:
        return None
    if 1_000_000_000 <= n <= 4_000_000_000:
        return datetime.fromtimestamp(n).date()
    return None


def _parse_date(text) -> date | None:
    """把 '2026-11-02' / '9/26/2025' / '2026-11-03 05:51:02' / unix 时间戳解析成 date。

    顺序: 先按看得见的日期字符串, 再退回 unix 时间戳。
    """
    found = _parse_date_explicit(text)
    if found is not None:
        return found
    return _parse_date_timestamp(text)


def _cn_today() -> date:
    """A 股所在时区的今天 (失败时退回系统本地日期)。"""
    try:
        from app.market_time import cn_today

        return cn_today()
    except Exception:  # noqa: BLE001 — 时间模块不可用时退回系统日期, 不能因此不返回雷达
        return date.today()


def _age_days(as_of: date | None, today: date) -> int | None:
    if as_of is None:
        return None
    return (today - as_of).days


# ── 打分与映射 ──────────────────────────────────────────
def index_score(chg_pct, ma20_pct=None) -> float | None:
    """单个指数的风险分 (0~100, 50=中性, 越高越危险)。

    chg_pct: 当日涨跌幅%
    ma20_pct: 相对 20 日均线的偏离% (正=在均线上方)。缺失时按 0 处理。
    """
    c = _f(chg_pct)
    if c is None:
        return None
    c = _clamp(c, -CHG_CAP, CHG_CAP)
    score = 50.0 - c * CHG_K
    m = _f(ma20_pct)
    if m is not None:
        # 均线上方 = 趋势健康 = 风险分降低, 但最多影响 MA_CAP 分
        score += _clamp(-m * MA_K, -MA_CAP, MA_CAP)
    return round(_clamp(score, 0.0, 100.0), 1)


def score_level(score) -> str:
    """风险分 → 等级。"""
    s = _f(score)
    if s is None:
        return "unknown"
    if s >= 75.0:
        return "high"
    if s >= 55.0:
        return "elevated"
    if s >= 30.0:
        return "normal"
    return "low"


def score_to_exposure(score) -> float | None:
    """风险分 → 仓位系数 (夹 [EXPOSURE_FLOOR, EXPOSURE_CAP])。

    50 分 → 1.00; 每高 1 分减 0.013; 每低 1 分加 0.010 (下跌的「加分」比
    上涨的「减分」慢, 因为低风险不该放大杠杆)。
    """
    s = _f(score)
    if s is None:
        return None
    raw = 1.0 - max(0.0, s - 50.0) * 0.013 + max(0.0, 50.0 - s) * 0.010
    return round(_clamp(raw, EXPOSURE_FLOOR, EXPOSURE_CAP), 3)


def drawdown_factor(drawdown_pct) -> float:
    """账户当前回撤% → 系数 (阶梯 + 地板, 永不归零)。"""
    dd = _f(drawdown_pct)
    if dd is None or dd <= 0:
        return 1.0
    for threshold, factor in DD_STEPS:
        if dd <= threshold:
            return factor
    # 超过最深阶梯: 按最深阶梯继续外推, 但不低于地板
    deepest_thr, deepest = DD_STEPS[-1]
    extra = (dd - deepest_thr) * 0.02
    return round(max(DD_FLOOR_FACTOR, deepest - extra), 3)


# ── 取数: 新浪批量快照 ──────────────────────────────────
def _sina_fetch(codes: list[str], *, timeout: float = 8.0) -> dict[str, list[str]]:
    """拉新浪 hq 批量行情 → {code: 字段列表}。失败返回空 dict, 不抛异常。"""
    if not codes:
        return {}
    out: dict[str, list[str]] = {}
    try:
        with httpx.Client(headers=_SINA_UA, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(_SINA_HQ + ",".join(codes))
        if resp.status_code != 200:
            logger.info("风险雷达: 新浪行情 HTTP %s", resp.status_code)
            return {}
        # 新浪一律 GB18030; 不能用 resp.text (httpx 会按头/utf-8 猜, 中文乱码)
        body = resp.content.decode("gb18030", errors="replace")
    except Exception as e:  # noqa: BLE001 — 网络失败按「无数据」降级, 不能阻断雷达
        logger.info("风险雷达: 新浪行情请求失败 (%s)", e)
        return {}
    for line in body.split(";"):
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        try:
            head, rest = line.split("=", 1)
            key = head[len("var hq_str_"):].strip()
            payload = rest.strip().strip('"')
            if payload:
                out[key] = payload.split(",")
        except ValueError:
            continue
    return out


def _parse_sina_gb(fields: list[str]) -> dict:
    """新浪美股 gb_* 行 → {price, chg_pct, as_of}。

    字段: 名称, 现价, 涨跌%, 时间, 涨跌额, 开盘, 最高, 最低, 52周高, 52周低, ...

    **`as_of` 要做美东交易日换算**: 新浪给的时间 (字段[3]) 是**北京时间**,
    实测 "2026-11-04 06:30:00" 对应的是美股 11-03 那个交易日的收盘。
    若直接取日期, 美股隔夜数据会被标成「今天」—— 用户就分不清看的是今天还是
    昨天的行情 (这正是本轮要解决的问题)。规则: 北京时间中午 12 点之前的时刻,
    其交易日算**前一天** (美股收盘落在北京时间 04:00~07:00)。
    """
    if len(fields) < 4:
        return {}
    return {
        "name": fields[0].strip(),
        "price": _f(fields[1]),
        "chg_pct": _f(fields[2]),
        "as_of": _us_trading_date(fields[3]),
        "source": "sina_gb",
    }


def _us_trading_date(text) -> date | None:
    """新浪美股时间 (北京时间戳) → 美股交易日。

    见 `_parse_sina_gb`: 北京时间 12:00 前算前一个交易日。只有日期串 (无时辰) 时
    不做换算 —— 此时的日期本身就是源方想表达的交易日。
    """
    if text is None:
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.date() - timedelta(days=1) if dt.hour < 12 else dt.date()
    return _parse_date(s)


def _em_quotes(secids: list[str], *, timeout: float = 8.0) -> dict[str, dict]:
    """东财批量实时快照 → {secid: {price, chg_pct, prev_close}}。失败返回 {}, 不抛异常。

    存在的理由: 新浪 `znb_TWSE` 的源停更一年多、`znb_HSI` 的涨跌幅恒为 0.00,
    这两处靠新浪补不上。东财 `ulist.np` 一次请求就能拿到全部 7 个指数的现价与
    涨跌幅 (实测 2026-11-03 返回 rc=0 正常)。

    东财会按 IP 间歇封禁 (实测返回 `{"rc":102,"data":null}`), 所以:
    - 只在需要兜底时调用, 且失败静默 (拿不到只是缺一个指数, 不进分数)。
    - 字段值可能是字符串 "-" (停市/无数据), `_f` 会把它变成 None, 天然安全。
    """
    if not secids:
        return {}
    try:
        with httpx.Client(headers=_EM_UA, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(
                _EM_QUOTE,
                params={
                    "secids": ",".join(secids),
                    "fields": "f2,f3,f18,f12,f14",
                    "invt": "2",
                    "fltt": "2",
                },
            )
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — 备用通道, 失败即当没有
        logger.info("风险雷达: 东财快照请求失败 (%s)", e)
        return {}
    data = (payload or {}).get("data") or {}
    rows = data.get("diff") or []
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("f12") or "").strip()
        if not code:
            continue
        # 反查 secid: secids 里形如 "100.TWII", 用后缀匹配
        hit = next((s for s in secids if s.endswith("." + code)), None)
        if hit is None:
            continue
        out[hit] = {
            "name": str(row.get("f14") or "").strip() or None,
            "price": _f(row.get("f2")),
            "chg_pct": _f(row.get("f3")),
            "prev_close": _f(row.get("f18")),
        }
    return out


def _parse_sina_znb(fields: list[str], today: date) -> dict:
    """新浪 znb_* 行 → {price, chg_pct, as_of}。

    字段布局实测 (2026-11-03, 部分标的字段更短, 必须按长度容错):
      富时/日经 (13 列): 名称, 现价, 涨跌额, 涨跌%, 时间, unix时间戳, 日期, 时间2, 昨收, ...
      TWSE / HSI (6 列): 名称, 现价, 涨跌额, 涨跌%, 日期或时间, unix时间戳
    日期可能落在 [4]~[7] 任意一列, 所以逐列试探解析 (含 unix 秒)。

    台湾 TWSE 的新浪源一年多没更新 (实测停在 2025-09-26), 必须按日期校验后丢弃 ——
    否则会把「一年前的 25580 点」当成今天的行情参与打分, 这是静默错数。
    """
    if len(fields) < 4:
        return {}
    price = _f(fields[1])
    chg = _f(fields[3])
    as_of = None
    # 先在所有可见列里找「日期字符串」, 找不到才退回 unix 时间戳 ——
    # 实测 znb 的第 5 列时间戳与第 6 列日期字符串互相矛盾 (时间戳偏差一年),
    # 日期字符串才是真实行情日期, 时间戳只能兜底。
    for raw in fields[4:10]:
        as_of = _parse_date_explicit(raw)
        if as_of is not None:
            break
    if as_of is None:
        for raw in fields[4:10]:
            as_of = _parse_date_timestamp(raw)
            if as_of is not None:
                break
    age = _age_days(as_of, today)
    if age is not None and age > STALE_DAYS:
        logger.info("风险雷达: 新浪 znb 数据过期 %s 天, 丢弃 (%s)", age, fields[0])
        return {"stale": True, "as_of": as_of, "name": fields[0].strip()}
    return {
        "name": fields[0].strip(),
        "price": price,
        "chg_pct": chg,
        "as_of": as_of,
        "source": "sina_znb",
    }


def _parse_sina_hf(fields: list[str]) -> dict:
    """新浪 hf_* (外盘期货) 行 → {price, prev, chg_pct}。

    字段: 现价, ?, 买价, 卖价, 最高, 最低, 时间, 昨收, 开盘, 持仓, ...
    涨跌幅不用源里的字段, 自己按 price/prev 重算 (与 premarket 口径一致)。
    """
    if len(fields) < 8:
        return {}
    price = _f(fields[0])
    prev = _f(fields[7])
    chg = None
    if price is not None and prev not in (None, 0):
        chg = round((price / prev - 1.0) * 100.0, 3)
    return {"price": price, "prev_close": prev, "chg_pct": chg}


def _parse_sina_fx(fields: list[str]) -> dict:
    """新浪 fx_* (外汇) 行 → {price, prev, chg_pct, note}。

    字段: 时间, 买价, 卖价, 昨收, ... 涨跌幅按 price/prev 重算
    (源里的涨跌幅字段实测不可靠: [10]=0.03 而真实 0.027%)。
    """
    if len(fields) < 4:
        return {}
    price = _f(fields[1])
    prev = _f(fields[3])
    chg = None
    if price is not None and prev not in (None, 0):
        chg = round((price / prev - 1.0) * 100.0, 3)
    return {"price": price, "prev_close": prev, "chg_pct": chg, "time": fields[0]}


# ── 取数: 20 日均线 ─────────────────────────────────────
def _em_kline(secid: str, *, lmt: int = 30, timeout: float = 8.0) -> list[float]:
    """东财单只日线收盘序列 (最近 lmt 根)。失败返回 [], 不抛异常。

    东财按 IP 间歇封禁 (实测返回 {"rc":102,"data":null}), 所以本函数只在需要
    算 MA20 时调用, 且失败静默 —— 拿不到均线只是少一个 ±10 分的修正项。
    """
    try:
        with httpx.Client(headers=_EM_UA, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(
                _EM_KLINE,
                params={
                    "secid": secid,
                    "fields1": "f1,f2,f3",
                    "fields2": "f51,f52,f53,f54,f55",
                    "klt": "101",
                    "fqt": "0",
                    "end": "20500101",
                    "lmt": str(lmt),
                },
            )
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — 东财限流/网络失败都只是缺均线
        logger.info("风险雷达: 东财日线失败 %s (%s)", secid, e)
        return []
    data = (payload or {}).get("data") or {}
    klines = data.get("klines") or []
    closes: list[float] = []
    for row in klines:
        parts = str(row).split(",")
        if len(parts) >= 3:
            v = _f(parts[2])
            if v is not None:
                closes.append(v)
    return closes


def _sina_us_daily(symbol: str, *, timeout: float = 10.0) -> list[dict]:
    """新浪美股日线 [{d,o,h,l,c,v}]。失败返回 []。"""
    try:
        with httpx.Client(headers=_SINA_UA, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(_SINA_US_DAILY, params={"symbol": symbol})
        rows = resp.json()
    except Exception as e:  # noqa: BLE001 — 备用通道, 失败即当没有
        logger.info("风险雷达: 新浪美股日线失败 %s (%s)", symbol, e)
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("c") is not None:
            out.append(r)
    return out


def _sina_futures_daily(symbol: str, *, timeout: float = 12.0) -> list[float]:
    """新浪全球期货日线 → 收盘序列。失败或空返回 []。

    为什么用期货日线算股指的 MA20: 东财 `push2his` 在部分网络环境 (实测群晖 NAS
    容器) 被整体阻断, Yahoo 也 403, 此时「相对 20 日均线的偏离」这一项就没数据了。
    新浪全球期货接口对 NK(日经) / HSI(恒生) / YM(道指) / ES(标普) / NQ(纳指) 都返回
    2000+ 根日线, 与其现货指数的 20 日均线偏离差异极小 (同标的、同方向), 用来做
    ±10 分的修正项完全够用。**只用来算均线, 不用它的价格与涨跌幅**(那两个仍取现货)。
    实测 2026-11-03: N225 现货 63081, NK 期货 close 62615 (差 0.7%, 均线偏离同为负)。
    """
    try:
        with httpx.Client(headers=_SINA_UA, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(_SINA_GLOBAL_FUT, params={"symbol": symbol})
        text = resp.content.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — 备用通道, 失败即当没有均线
        logger.info("风险雷达: 新浪期货日线失败 %s (%s)", symbol, e)
        return []
    if "(" not in text or ")" not in text:
        return []
    try:
        rows = _json.loads(text[text.index("(") + 1: text.rindex(")")])
    except Exception as e:  # noqa: BLE001 — JSONP 体解析失败也只是没均线
        logger.info("风险雷达: 新浪期货日线解析失败 %s (%s)", symbol, e)
        return []
    if not isinstance(rows, list):
        return []
    out: list[float] = []
    for r in rows:
        if isinstance(r, dict):
            v = _f(r.get("close"))
            if v is not None and v > 0:
                out.append(v)
    return out


def ma20_pct(closes: list[float]) -> float | None:
    """收盘序列 → 最新价相对 20 日均线的偏离%。少于 20 根返回 None。"""
    if not closes or len(closes) < 20:
        return None
    window = closes[-20:]
    avg = sum(window) / 20.0
    if avg <= 0:
        return None
    return round((closes[-1] / avg - 1.0) * 100.0, 2)


# ── 组装 ────────────────────────────────────────────────
def _fetch_indexes(today: date) -> tuple[list[dict], list[str]]:
    """取 7 个环球指数的当前快照 + MA20 偏离。返回 (rows, missing_codes)。

    取数策略: 先用新浪批量 (一次请求拿全部现价 + 涨跌幅, 便宜且稳),
    新浪缺/过期的再用东财快照兜底 (台湾 TWSE 的新浪源停更、恒生的新浪涨幅恒 0),
    最后对每个指数单独补 MA20 (东财单只; 美股失败回落新浪美股日线)。
    """
    sina_codes: list[str] = []
    for idx in GLOBAL_INDEXES:
        if idx.get("sina_gb"):
            sina_codes.append(idx["sina_gb"])
        if idx.get("sina_znb"):
            sina_codes.append(idx["sina_znb"])
    bulk = _sina_fetch(sina_codes)

    # 先把新浪能给的都解析出来, 收集「没有可用价格」的那些 → 交给东财兜底
    parsed: dict[str, dict] = {}
    for idx in GLOBAL_INDEXES:
        base: dict = {}
        if idx.get("sina_gb"):
            base = _parse_sina_gb(bulk.get(idx["sina_gb"]) or [])
        if not base and idx.get("sina_znb"):
            base = _parse_sina_znb(bulk.get(idx["sina_znb"]) or [], today)
        parsed[idx["code"]] = base
    need_em = [
        idx["em_secid"]
        for idx in GLOBAL_INDEXES
        if idx.get("em_secid")
        and (
            # 价格缺失 / 标了过期 / 涨跌幅缺失或恒 0 (恒生那种)
            _f(parsed[idx["code"]].get("price")) is None
            or parsed[idx["code"]].get("stale")
            or _f(parsed[idx["code"]].get("chg_pct")) in (None, 0.0)
        )
    ]
    em_rows = _em_quotes(need_em) if need_em else {}

    rows: list[dict] = []
    missing: list[str] = []
    for idx in GLOBAL_INDEXES:
        base = parsed[idx["code"]]
        stale = bool(base.get("stale"))
        price = _f(base.get("price"))
        chg = _f(base.get("chg_pct"))
        as_of = base.get("as_of")
        source = base.get("source") or ""
        # 东财兜底: 新浪没价 / 过期 / 涨幅为 0 (恒生的新浪源涨跌幅恒 0.00 是假的)
        em = em_rows.get(idx.get("em_secid") or "") or {}
        em_price = _f(em.get("price"))
        em_chg = _f(em.get("chg_pct"))
        if em_price is not None and (
            price is None or stale or chg in (None, 0.0)
        ):
            price = em_price
            if em_chg is not None:
                chg = em_chg
            stale = False
            as_of = today  # 东财快照必是当日, 但源不提供明确日期字段
            source = f"{source}+em_q" if source else "em_q"
        age = _age_days(as_of, today) if as_of else None
        if age is not None and age > STALE_DAYS:
            stale = True
        # 20 日均线: 东财优先, 美股再回落新浪日线
        closes = _em_kline(idx["em_secid"]) if idx.get("em_secid") else []
        ma_source = "em_k"
        if not closes and idx.get("sina_us"):
            closes = [(_f(r.get("c")) or 0.0) for r in _sina_us_daily(idx["sina_us"])]
            ma_source = "sina_us_k"
        if not closes and idx.get("sina_fut"):
            closes = _sina_futures_daily(idx["sina_fut"])
            ma_source = "sina_fut_k"
        ma = ma20_pct([c for c in closes if c > 0]) if closes else None
        if ma is not None and ma_source != "em_k":
            source = f"{source}+{ma_source}" if source else ma_source
        elif ma is not None:
            source = f"{source}+em" if source else "em"
        usable = (not stale) and price is not None and chg is not None
        row = {
            "code": idx["code"],
            "name": em.get("name") or base.get("name") or idx["name"],
            "region": idx["region"],
            "weight": idx["weight"],
            "price": price,
            "chg_pct": chg,
            "ma20_pct": ma,
            "score": index_score(chg, ma) if usable else None,
            "as_of": as_of.isoformat() if as_of else None,
            "source": source if usable else "missing",
            "stale": stale,
        }
        rows.append(row)
        if not usable:
            missing.append(idx["code"])
    return rows, missing


def _region_summary(rows: list[dict], region: str) -> dict:
    """把一个区域的指数行合成区域分与区域仓位系数。"""
    picked = [r for r in rows if r["region"] == region]
    weighted = 0.0
    total_w = 0.0
    for r in picked:
        s = _f(r.get("score"))
        if s is None:
            continue
        w = float(r.get("weight") or 0.0)
        weighted += s * w
        total_w += w
    score = round(weighted / total_w, 1) if total_w > 0 else None
    return {
        "score": score,
        "exposure": score_to_exposure(score),
        "indexes": picked,
    }


def _breadth(data_dir: Path) -> dict:
    """A 股宽度: 用已落盘的最近两个交易日 enriched 分区算涨跌家数。

    复用已缓存的全市场快照 (零额外网络)。用途是识破「指数被权重股拉住、
    4000 只里 3500 只在跌」的普跌掩护 —— 单看指数会以为没事。
    """
    empty = {"ok": False, "score": None, "dn_ratio": None, "up": 0, "down": 0, "total": 0}
    try:
        import polars as pl

        base = Path(data_dir) / "kline_daily_enriched"
        if not base.exists():
            return empty
        parts = sorted(p for p in base.glob("date=*") if p.is_dir())
        if len(parts) < 2:
            return empty
        last = pl.read_parquet(parts[-1] / "part.parquet")
        prev = pl.read_parquet(parts[-2] / "part.parquet")
        if last.is_empty() or prev.is_empty():
            return empty
        joined = last.select(["symbol", "close"]).join(
            prev.select(["symbol", pl.col("close").alias("prev")]), on="symbol", how="inner"
        ).filter(pl.col("prev") > 0)
        if joined.is_empty():
            return empty
        up = joined.filter(pl.col("close") > pl.col("prev")).height
        down = joined.filter(pl.col("close") < pl.col("prev")).height
        total = joined.height
    except Exception as e:  # noqa: BLE001 — 缺分区/parquet 损坏都按「无宽度数据」
        logger.info("风险雷达: A股宽度计算失败 (%s)", e)
        return empty
    if total < 500:
        return {"ok": False, "score": None, "dn_ratio": None, "up": up, "down": down, "total": total}
    dn_ratio = down / total
    score = round(_clamp(50.0 + (dn_ratio - 0.5) * 100.0, 0.0, 100.0), 1)
    return {
        "ok": True,
        "score": score,
        "dn_ratio": round(dn_ratio, 4),
        "up": up,
        "down": down,
        "total": total,
    }


def _equity(data_dir: Path, account_id: str) -> dict:
    """账户当前回撤 (窗口内峰值 → 现在) → 系数。

    刻意用「当前回撤」而不是历史最大回撤: 最大回撤一旦深过就永久缩仓,
    账户永远恢复不了 —— 那是个只会单向收紧的棘手指标。
    """
    empty = {"ok": False, "factor": 1.0, "drawdown_pct": None, "window_days": DD_WINDOW_DAYS}
    try:
        from app.strategy import paper

        nav = paper.load_nav(Path(data_dir), account_id)
    except Exception as e:  # noqa: BLE001 — 无净值/账户不存在都按「无回撤信号」
        logger.info("风险雷达: 读取净值失败 (%s)", e)
        return empty
    if not nav:
        return empty
    points = []
    for row in nav[-DD_WINDOW_DAYS:]:
        v = _f((row or {}).get("nav"))
        if v is not None and v > 0:
            points.append(v)
    if len(points) < 2:
        return empty
    peak = max(points)
    cur = points[-1]
    dd = round(max(0.0, (peak - cur) / peak * 100.0), 2)
    return {
        "ok": True,
        "factor": drawdown_factor(dd),
        "drawdown_pct": dd,
        "window_days": DD_WINDOW_DAYS,
    }


def _advice(
    score: float | None,
    level: str,
    exposure: float,
    regions: dict,
    breadth: dict,
    equity: dict,
    missing: list[str],
) -> str:
    """把分数翻译成一句人话。"""
    if score is None:
        return "外围行情全部缺失, 风险雷达不可用, 仓位按无风险信号处理 (系数 1.00)。"
    parts: list[str] = []
    label = LEVEL_TEXT.get(level, level)
    # 一位小数与前端主数值同口径: 卡片显示 69.6, 文案也必须是 69.6,
    # 否则同一张卡上两个数字对不上, 用户会怀疑哪个是对的。
    parts.append(f"综合风险 {score:.1f} 分 ({label}), 仓位系数 ×{exposure:.2f}")
    weak = [name for name in ("us", "asia", "hk") if (regions.get(name) or {}).get("score") is None]
    if weak:
        cn = {"us": "美股", "asia": "亚太", "hk": "恒生"}
        parts.append("缺失: " + "、".join(cn.get(w, w) for w in weak))
    if breadth.get("ok"):
        dn = breadth.get("dn_ratio") or 0.0
        if dn >= 0.65:
            parts.append(f"A 股 {dn * 100:.0f}% 在跌, 宽度很差, 别只看指数")
        elif dn <= 0.35:
            parts.append(f"A 股仅 {dn * 100:.0f}% 在跌, 宽度健康")
    if equity.get("ok") and (equity.get("drawdown_pct") or 0) >= 5:
        parts.append(f"账户当前回撤 {equity['drawdown_pct']:.1f}%, 系数降到 ×{equity.get('factor')}")
    if missing:
        parts.append("未取到: " + "、".join(missing))
    return "; ".join(parts) + "。"


def _overall_as_of(rows: list[dict], today: date) -> tuple[str | None, int | None]:
    """这批外围数据对应的**数据日期** (不是抓取时刻) + 滞后自然日数。

    规则: 只看「可用且未过期」的指数行, 取其中最新的 `as_of`。

    - `stale=True` 的行被丢弃 (它已经不符合打分条件), 不能拿旧数据冒充最新;
    - 没有 `as_of` 的行 (取数失败 / 源不提供日期) 不参与;
    - 一行都没有 → `(None, None)`, 前端显示「数据缺失」而不是假日期。

    为什么要单独给这个字段: `fetched_at` 是「什么时候抓的」, 而这个字段是
    「数据本身是哪天的」。美股隔夜数据在 A 股当天上午被抓到时, 两者会差一天 ——
    用户要能分辨自己看的是今天的行情还是昨天的。
    """
    best: date | None = None
    for row in rows:
        if row.get("stale"):
            continue
        if row.get("price") is None or row.get("chg_pct") is None:
            continue
        raw = row.get("as_of")
        if not raw:
            continue
        try:
            d = raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    if best is None:
        return None, None
    return best.isoformat(), _age_days(best, today)


def _build(data_dir: Path, account_id: str, allow_fetch: bool) -> dict:
    """实际组装雷达 (不读缓存)。"""
    today = _cn_today()
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if allow_fetch:
        rows, missing = _fetch_indexes(today)
    else:
        rows = [
            {
                "code": idx["code"],
                "name": idx["name"],
                "region": idx["region"],
                "weight": idx["weight"],
                "price": None,
                "chg_pct": None,
                "ma20_pct": None,
                "score": None,
                "as_of": None,
                "source": "missing",
                "stale": False,
            }
            for idx in GLOBAL_INDEXES
        ]
        missing = [idx["code"] for idx in GLOBAL_INDEXES]

    regions = {name: _region_summary(rows, name) for name in ("us", "asia", "hk")}

    # 区域分 → 环球分 (按区域权重; 缺失区域不摊薄, 只按有值的区域重新归一)
    weighted = 0.0
    total_w = 0.0
    for name, payload in regions.items():
        s = _f(payload.get("score"))
        if s is None:
            continue
        w = REGION_WEIGHT.get(name, 0.0)
        weighted += s * w
        total_w += w
    global_score = round(weighted / total_w, 1) if total_w > 0 else None
    global_exposure = score_to_exposure(global_score)

    breadth = _breadth(data_dir)
    equity = _equity(data_dir, account_id)

    # 合成: 外围系数 × 账户回撤系数, 再夹回 [FLOOR, CAP]
    base = global_exposure if global_exposure is not None else 1.0
    exposure = round(_clamp(base * float(equity.get("factor") or 1.0), EXPOSURE_FLOOR, EXPOSURE_CAP), 3)
    level = score_level(global_score)

    ok = global_score is not None
    as_of, stale_days = _overall_as_of(rows, today)
    return {
        "ok": ok,
        "score": global_score,
        "level": level,
        "level_text": LEVEL_TEXT.get(level, "未知"),
        "exposure": exposure,
        "global_exposure": global_exposure,
        "regions": regions,
        "indexes": rows,
        "breadth": breadth,
        "equity": equity,
        "missing": missing,
        "advice": _advice(global_score, level, exposure, regions, breadth, equity, missing),
        "as_of": as_of,
        "stale_days": stale_days,
        "fetched_at": fetched_at,
    }


def radar_snapshot(
    data_dir: Path,
    *,
    account_id: str = "default",
    allow_fetch: bool = True,
    use_cache: bool = True,
) -> dict:
    """风险雷达快照 (供 API 与模拟盘仓位系数使用)。

    Args:
        data_dir: 数据根目录 (读 A 股宽度分区、账户净值、响应缓存)。
        account_id: 模拟盘账户 (决定「账户回撤」这一路系数)。不存在则按无回撤处理。
        allow_fetch: False 时不联网, 只返回本地能算的部分 (测试/离线用)。
        use_cache: False 时强制重算 (手动刷新用)。

    任何网络/数据失败都返回 ok=False 的字典, 不抛异常 —— 调用方 (模拟盘钩子)
    不能因为雷达不可用而停摆。
    """
    key = f"{Path(data_dir).as_posix()}|{account_id}|{int(bool(allow_fetch))}"
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL:
            return hit[1]
    try:
        payload = _build(Path(data_dir), account_id, allow_fetch)
    except Exception as e:  # noqa: BLE001 — 雷达整体失败必须降级成 ok=False
        logger.warning("风险雷达组装失败 (降级为无风险信号): %s", e)
        payload = {
            "ok": False,
            "score": None,
            "level": "unknown",
            "level_text": "未知",
            "exposure": 1.0,
            "global_exposure": None,
            "regions": {},
            "indexes": [],
            "breadth": {"ok": False, "score": None, "dn_ratio": None, "up": 0, "down": 0, "total": 0},
            "equity": {"ok": False, "factor": 1.0, "drawdown_pct": None, "window_days": DD_WINDOW_DAYS},
            "missing": [],
            "advice": f"风险雷达组装失败 ({type(e).__name__}), 仓位按无风险信号处理。",
            "as_of": None,
            "stale_days": None,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = (now, payload)
    return payload


def invalidate_cache() -> None:
    """清空雷达缓存 (设置变更/手动刷新时调用)。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def exposure_for_paper(data_dir: Path, account_id: str = "default") -> tuple[float, str]:
    """给模拟盘用的两个系数: (外围系数, 账户回撤系数)。

    刻意**不返回**合成后的 `exposure`: 合成值已经乘过大盘情绪系数, 而交易计划
    那边已经按大盘算过一次, 同一份风险计两次会过度缩仓; 更糟的是它被夹在
    [0.35, 1.10] 内, 大盘弱势时合成值本就贴着 0.35, 再除以大盘系数恒等于 1,
    恰好在最需要缩仓的时候把缩仓信号抹掉。

    返回 (1.0, 1.0) 表示无风险信号 (不加不减)。
    """
    snap = radar_snapshot(data_dir, account_id=account_id)
    if not snap.get("ok"):
        return 1.0, 1.0
    global_exp = _f(snap.get("global_exposure"))
    eq = snap.get("equity") or {}
    eq_factor = _f(eq.get("factor"))
    return (
        global_exp if global_exp is not None else 1.0,
        eq_factor if eq_factor is not None else 1.0,
    )


def cache_stats() -> dict:
    """缓存状况 (诊断用)。"""
    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "ttl": CACHE_TTL}


__all__ = [
    "CACHE_TTL",
    "DD_FLOOR_FACTOR",
    "DD_STEPS",
    "DD_WINDOW_DAYS",
    "EXPOSURE_CAP",
    "EXPOSURE_FLOOR",
    "GLOBAL_INDEXES",
    "LEVEL_TEXT",
    "REGION_WEIGHT",
    "STALE_DAYS",
    "cache_stats",
    "drawdown_factor",
    "exposure_for_paper",
    "index_score",
    "invalidate_cache",
    "ma20_pct",
    "radar_snapshot",
    "score_level",
    "score_to_exposure",
]
