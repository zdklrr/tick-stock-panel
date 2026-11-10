"""盘前简报 (premarket)——用外围隔夜行情给 A 股当天开盘一个「方向 + 概率 + 缺口区间」。

职责边界: 只做取数与统计, 不落盘、不下单。HTTP 暴露在 app/api/risk.py,
模拟盘可在建计划时读 `bias` 做提示 (不直接改股数, 改股数是 global_risk 的职责)。

## 为什么用这套权重 (而不是「美股跌了就低开」)

A50 期货打头 (W_A50=0.65): 富时中国 A50 期货比 A 股早开盘、且近乎全天连续,
它反映的是外资对 A 股当天的定价。关键是 —— **美股隔夜的影响已经被 A50 部分
吸收了**。如果直接把「美股 20% + 亚太 15% + A50 65%」再简单加权一遍, 等于把
美股的风险算两次。所以 A50 存在时它占绝对主导, 美股/亚太只是补 A50 没覆盖的
部分; A50 取不到时才退回「美股 0.55 + 亚太 0.45」的纯外围合成。

## 概率是算出来的, 不是编的

合成信号 S% (权重加权后的全球涨跌幅) 经 `GAP_BETA=0.50` 传导成开盘缺口期望,
缺口分布按 N(S*GAP_BETA, GAP_SIGMA) 处理, 再切成「高开 / 平开 / 低开」三段
取概率。没有黑盒、没有拟合参数、没有机器学习 —— 用户能自己复算。

## 两道诚实性约束 (首测后加的, 必须有)

- `CHG_CAP=3.0`: 单个指数涨跌幅先夹 ±3%。
- `S_CAP=1.2`: 合成信号整体夹 ±1.2%。

起因: 实测某日 KOSPI +5.63%、亚太合成 +2.99%, 把 S 推到 0.82; 若按
GAP_SIGMA=0.35 计算会得出「P(高开)=90.5%」。对相关系数只有 0.2~0.3 的弱信号,
这是过度自信。两道约束把最极端情况压回到 ~77%, 更接近真实可信度。

## 数据源 (实测可用, 2026-11-03)
- 富时中国 A50 期货: 新浪 `hf_CHA50CFD` (hf_* 字段里现价在 [0], 昨收在 [7])。
- 美股/亚太: 复用 `app.services.global_risk._sina_fetch` 的同一批接口
  (`gb_$ixic` 等 / `znb_NKY` 等), 不重复写解析逻辑。
- 离岸人民币: 新浪 `fx_susdcnh`。**不进加权**: 对开盘缺口的解释力太弱,
  硬塞一个系数等于编数据; 只作风险提示。
  涨跌幅自己按 price/prev 重算 (源里字段实测不可靠)。
- 预计振幅: 用 510300 (沪深300ETF) 的 ATR20; 取不到时用 AMPL_FALLBACK=1.10。

## 缓存
`_TTL=120` 秒。前端轮询 / 页面刷新会反复打到这几个接口, 不能每次都联网。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime

from app.services import global_risk
from app.services.global_risk import (
    _f,
    _parse_sina_fx,
    _parse_sina_gb,
    _parse_sina_hf,
    _sina_fetch,
)

logger = logging.getLogger(__name__)

# ── 权重与阈值 ──────────────────────────────────────────
#: A50 在权重里的主导地位 (美股影响已被 A50 部分吸收, 见模块 docstring)。
W_A50 = 0.65
W_US_BG = 0.20
W_ASIA_BG = 0.15
#: A50 缺失时的退回权重。
W_US_ONLY = 0.55
W_ASIA_ONLY = 0.45

#: 美股内部权重 / 亚太内部权重 (与 global_risk 区域口径一致)。
US_WEIGHTS: dict[str, float] = {"IXIC": 0.50, "SPX": 0.30, "DJIA": 0.20}
ASIA_WEIGHTS: dict[str, float] = {"N225": 0.40, "KS11": 0.40, "TWII": 0.20}

#: 合成信号 S% → 开盘缺口% 的传导系数。
GAP_BETA = 0.50
#: 缺口残差标准差 (决定概率分布的宽窄)。
GAP_SIGMA = 0.55
#: |S| 小于此值算「震荡」。
BIAS_TH = 0.30
#: 缺口绝对值小于此值 (%) 算「平开」, 用于把概率分布切成三段。
FLAT_BAND = 0.20
#: 强度阈值。
STRONG_TH = 0.80
MID_TH = 0.40

#: 诚实性约束 (见模块 docstring 的两道约束)。
CHG_CAP = 3.0
S_CAP = 1.2

#: 510300 ATR20 取不到时的经验振幅。
AMPL_FALLBACK = 1.10

#: 缓存 TTL 秒。
_TTL = 120.0

_SINA_FX_CNH = "fx_susdcnh"
_SINA_HF_A50 = "hf_CHA50CFD"

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()

#: 新浪美股代码 (gb_* 批量)。
_US_SINA = {"DJIA": "gb_$dji", "IXIC": "gb_$ixic", "SPX": "gb_$inx"}
#: 新浪亚太代码 (znb_* 批量)。
_ASIA_SINA = {"N225": "znb_NKY", "KS11": "znb_KOSPI", "TWII": "znb_TWSE"}


def _latest_date(values: list[date | None]) -> date | None:
    """一组 date (可能含 None) → 其中最新的一天; 全空返回 None。

    用于把「某区域里各指数各自的数据日期」收敛成一个区域日期: 只取实际有值的,
    缺失的不参与 —— 否则一个取数失败的指数会把区域日期拖回 None 或旧日期。
    """
    picked = [v for v in values if v is not None]
    return max(picked) if picked else None


def _iso(d: date | None) -> str | None:
    """date → 'YYYY-MM-DD'; None 透传 (口径约定: as_of 只能是日期串或 None)。"""
    return d.isoformat() if d else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _weighted_chg(changes: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """按权重合成涨跌幅%; 缺失项不摊薄, 只对取到的项重新归一。"""
    total = 0.0
    total_w = 0.0
    for code, w in weights.items():
        v = _f(changes.get(code))
        if v is None:
            continue
        total += _clamp(v, -CHG_CAP, CHG_CAP) * w
        total_w += w
    if total_w <= 0:
        return None
    return total / total_w


def _norm_cdf(x: float) -> float:
    """标准正态 CDF (用 erf, 不引 scipy)。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def gap_probs(signal: float, *, sigma: float = GAP_SIGMA, flat_band: float = FLAT_BAND) -> dict:
    """合成信号 → 高开/平开/低开概率 (%, 和为 100)。

    模型: 缺口 ~ N(signal * GAP_BETA, sigma); |缺口| <= flat_band 算平开。
    完全透明, 用户能自己用计算器复算。
    """
    s = float(signal)
    mu = s * GAP_BETA
    lo = _norm_cdf((-flat_band - mu) / sigma)
    mid = _norm_cdf((flat_band - mu) / sigma) - lo
    hi = 1.0 - _norm_cdf((flat_band - mu) / sigma)
    up = round(hi * 100.0, 1)
    flat = round(mid * 100.0, 1)
    down = round(100.0 - up - flat, 1)
    return {"up": up, "flat": flat, "down": down}


def bias_of(signal: float) -> tuple[str, str]:
    """合成信号 → (中文方向, 英文键)。"""
    s = float(signal)
    if s >= BIAS_TH:
        return "偏多", "bull"
    if s <= -BIAS_TH:
        return "偏空", "bear"
    return "震荡", "flat"


def strength_of(signal: float) -> tuple[str, str]:
    """合成信号绝对值 → (中文强度, 英文键)。"""
    a = abs(float(signal))
    if a >= STRONG_TH:
        return "强", "strong"
    if a >= MID_TH:
        return "中", "mid"
    return "弱", "weak"


def _amplitude_atr_pct(closes: list[float]) -> float:
    """用日线收盘序列估计带动量振幅% (ATR/t 近似的百分比表达)。

    简化成「近 20 日平均振幅%」—— 只需要一个数量级正确的「今天大概会波动多少」,
    不需要真正的 ATR 口径 (真正的 ATR 要 OHLC, 而这里只取了 close)。
    取不到时返回 AMPL_FALLBACK。
    """
    pts = [v for v in (_f(c) for c in closes) if v is not None and v > 0]
    if len(pts) < 21:
        return AMPL_FALLBACK
    amps = []
    for i in range(-20, 0):
        prev = pts[i - 1]
        if prev > 0:
            amps.append(abs(pts[i] / prev - 1.0) * 100.0)
    if not amps:
        return AMPL_FALLBACK
    return round(sum(amps) / len(amps), 2)


def _fetch_raw() -> dict:
    """一次性拉齐盘前需要的全部原始数据 (失败项为 None, 不抛异常)。"""
    codes = list(_US_SINA.values()) + list(_ASIA_SINA.values()) + [_SINA_HF_A50, _SINA_FX_CNH]
    bulk = _sina_fetch(codes)
    return bulk


def _build(allow_fetch: bool) -> dict:
    """组装盘前简报 (不读缓存)。

    `as_of` 语义 (与 `fetched_at` 严格区分):
    - 顶层 `as_of` = **今天**。盘前简报问的是「今天开盘会怎么开」, 所以它的
      数据日期天然是今天; 若显示某个指数的隔夜日期, 用户反而会以为简报在讲昨天。
    - `us.as_of` = 美股隔夜那根K线的日期 (通常是上一个交易日)。
    - `asia.as_of` = 亚太同期数据的日期 (通常是今天, 东财兜底算当天)。
    - `a50.as_of` / `cnh.as_of` = 今天 (期货与外汇是实时报价)。
    这样前端既能说「这是今天的预判」, 又能标出「美股那 20% 权重用的是哪天收盘」。
    """
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 「今天」按 A 股所在时区取 (与 global_risk / market_timing 同一口径):
    # 服务器跑在 UTC (NAS) 时, 本地日期在北京时间 08:00 前会差一天, 直接 datetime.now()
    # 会让盘前简报的 as_of 退回昨天。
    today = global_risk._cn_today()
    today_iso = today.isoformat()
    empty = {
        "ok": False,
        "bias": "未知",
        "bias_key": "flat",
        "strength": "未知",
        "strength_key": "weak",
        "probs": {"up": 33.3, "flat": 33.3, "down": 33.3},
        "gap_pct": {"low": 0.0, "high": 0.0},
        "amplitude": AMPL_FALLBACK,
        "signal": 0.0,
        "a50": {"price": None, "chg_pct": None, "as_of": None},
        "us": {"chg_pct": None, "as_of": None},
        "asia": {"chg_pct": None, "as_of": None},
        "cnh": {"price": None, "chg_pct": None, "note": "", "as_of": None},
        "drivers": [],
        "advice": "盘前数据全部缺失, 无法判断开盘方向。",
        "as_of": today_iso,
        "fetched_at": fetched_at,
    }
    if not allow_fetch:
        return empty

    bulk = _fetch_raw()
    if not bulk:
        return empty

    # 美股 / 亚太各自合成 (同时收集各指数的数据日期, 用于算区域 as_of)
    us_changes: dict[str, float | None] = {}
    us_rows: list[dict] = []
    us_dates: list[date | None] = []
    for code, sina_key in _US_SINA.items():
        row = _parse_sina_gb(bulk.get(sina_key) or [])
        chg = _f(row.get("chg_pct"))
        us_changes[code] = chg
        if chg is not None:
            us_dates.append(row.get("as_of"))
        us_rows.append({"code": code, "name": row.get("name") or code, "chg_pct": chg})
    asia_changes: dict[str, float | None] = {}
    asia_rows: list[dict] = []
    asia_dates: list[date | None] = []
    # 东财兜底: 新浪的台湾 TWSE 源停更一年多 (实测停在 2025-09-26), 恒生涨幅恒 0,
    # 这两处必须用东财快照替换, 否则盘前合成里混进一年前的涨跌幅。
    em_asia = global_risk._em_quotes(
        [f"100.{code}" for code in _ASIA_SINA]
    )
    for code, sina_key in _ASIA_SINA.items():
        row = _parse_sina_znb_light(bulk.get(sina_key) or [])
        chg = _f(row.get("chg_pct"))
        name = row.get("name") or code
        as_of = row.get("as_of")
        em = em_asia.get(f"100.{code}") or {}
        em_chg = _f(em.get("chg_pct"))
        if em_chg is not None and (chg in (None, 0.0)):
            chg = em_chg
            name = em.get("name") or name
            as_of = today  # 东财快照不提供日期字段, 按当天算 (与 global_risk 同一口径)
        asia_changes[code] = chg
        if chg is not None:
            asia_dates.append(as_of)
        asia_rows.append({"code": code, "name": name, "chg_pct": chg})

    us_avg = _weighted_chg(us_changes, US_WEIGHTS)
    asia_avg = _weighted_chg(asia_changes, ASIA_WEIGHTS)

    # A50 期货
    a50 = _parse_sina_hf(bulk.get(_SINA_HF_A50) or [])
    a50_chg = _f(a50.get("chg_pct"))

    # 离岸人民币 (只用来看方向, 不进加权)
    cnh = _parse_sina_fx(bulk.get(_SINA_FX_CNH) or [])
    cnh_chg = _f(cnh.get("chg_pct"))
    cnh_note = ""
    if cnh_chg is not None:
        if cnh_chg > 0.05:
            cnh_note = "人民币贬值, 对外资流入偏不利 (仅提示, 不参与加权)"
        elif cnh_chg < -0.05:
            cnh_note = "人民币升值, 对外资流入偏有利 (仅提示, 不参与加权)"
        else:
            cnh_note = "人民币基本稳定 (仅提示, 不参与加权)"

    # 合成信号: A50 主导, 缺失时退回纯外围
    parts: list[tuple[str, float | None, float]] = []
    if a50_chg is not None:
        parts.append(("A50期货", a50_chg, W_A50))
        if us_avg is not None:
            parts.append(("美股", us_avg, W_US_BG))
        if asia_avg is not None:
            parts.append(("亚太", asia_avg, W_ASIA_BG))
    else:
        if us_avg is not None:
            parts.append(("美股", us_avg, W_US_ONLY))
        if asia_avg is not None:
            parts.append(("亚太", asia_avg, W_ASIA_ONLY))

    if not parts:
        return empty

    total = 0.0
    total_w = 0.0
    for _name, val, w in parts:
        if val is None:
            continue
        total += _clamp(val, -CHG_CAP, CHG_CAP) * w
        total_w += w
    if total_w <= 0:
        return empty
    signal = round(_clamp(total / total_w, -S_CAP, S_CAP), 3)

    bias, bias_key = bias_of(signal)
    strength, strength_key = strength_of(signal)
    probs = gap_probs(signal)
    mu = signal * GAP_BETA
    gap = {
        "low": round(mu - GAP_SIGMA, 2),
        "high": round(mu + GAP_SIGMA, 2),
    }
    amplitude = _amplitude_from_local_etf()
    if amplitude is None:
        amplitude = AMPL_FALLBACK

    drivers = []
    if a50_chg is not None:
        drivers.append(f"A50 期货 {a50_chg:+.2f}% (权重 {W_A50:.0%}, 外资对 A 股当天定价)")
    else:
        drivers.append("A50 期货缺失, 退回纯外围合成 (美股 55% + 亚太 45%)")
    if us_avg is not None:
        detail = "、".join(
            f"{r['name']} {r['chg_pct']:+.2f}%"
            for r in us_rows
            if r.get("chg_pct") is not None
        )
        drivers.append(f"美股隔夜 {us_avg:+.2f}% ({detail})")
    else:
        drivers.append("美股隔夜全缺")
    if asia_avg is not None:
        detail = "、".join(
            f"{r['name']} {r['chg_pct']:+.2f}%"
            for r in asia_rows
            if r.get("chg_pct") is not None
        )
        drivers.append(f"亚太同期 {asia_avg:+.2f}% ({detail})")
    else:
        drivers.append("亚太同期全缺")
    if cnh_chg is not None:
        drivers.append(f"离岸人民币 {cnh.get('price')} ({cnh_chg:+.2f}%) {cnh_note}")

    advice = (
        f"预计 {bias} ({strength}), 开盘缺口 {gap['low']:+.1f}% ~ {gap['high']:+.1f}%, "
        f"预计振幅 {amplitude:.1f}%; 高开 {probs['up']:.0f}% / 平开 {probs['flat']:.0f}% / "
        f"低开 {probs['down']:.0f}%。概率是按缺口正态分布切三段算出来的, 不是黑盒模型。"
    )

    return {
        "ok": True,
        "bias": bias,
        "bias_key": bias_key,
        "strength": strength,
        "strength_key": strength_key,
        "probs": probs,
        "gap_pct": gap,
        "amplitude": float(amplitude),
        "signal": signal,
        "a50": {"price": _f(a50.get("price")), "chg_pct": a50_chg, "as_of": today_iso},
        "us": {
            "chg_pct": round(us_avg, 3) if us_avg is not None else None,
            "rows": us_rows,
            "as_of": _iso(_latest_date(us_dates)),
        },
        "asia": {
            "chg_pct": round(asia_avg, 3) if asia_avg is not None else None,
            "rows": asia_rows,
            "as_of": _iso(_latest_date(asia_dates)),
        },
        "cnh": {
            "price": _f(cnh.get("price")),
            "chg_pct": cnh_chg,
            "note": cnh_note,
            "as_of": today_iso,
        },
        "drivers": drivers,
        "advice": advice,
        "as_of": today_iso,
        "fetched_at": fetched_at,
    }


def _parse_sina_znb_light(fields: list[str], today=None) -> dict:
    """新浪 znb_* 轻量解析 (名称 + 涨跌幅 + 数据日期 + 新鲜度校验)。

    必须做新鲜度校验: 台湾 TWSE 的新浪源实测停在 2025-09-26, 若不校验就会把
    一年多前的 -1.70% 当成今天的亚太涨跌幅算进开盘缺口 —— 这是静默错数。
    (早期版本刻意不校验, 理由是「盘前只看方向, 稍旧也不会造成仓位错误」;
     实测发现台湾这份数据旧了一年多, 涨跌幅根本是另一段行情, 该理由不成立。)

    返回的 `as_of` 是该指数的**数据日期**, 供上层汇总出区域日期 (as_of) 用;
    过期被丢弃时一律不给 `as_of` —— 丢弃的数据不该参与日期计算。
    """
    if len(fields) < 4:
        return {}
    base = global_risk._parse_sina_znb(fields, today or global_risk._cn_today())
    if not base or base.get("stale"):
        return {}
    return {
        "name": base.get("name") or fields[0].strip(),
        "price": base.get("price"),
        "chg_pct": base.get("chg_pct"),
        "as_of": base.get("as_of"),
    }


def _amplitude_from_local_etf() -> float | None:
    """用本地 510300 日线估计预计振幅; 数据目录不可用时返回 None。"""
    try:
        from app.services import global_risk as gr

        # 复用 global_risk 的东财取数 (单只 30 根日线), 失败即 None
        closes = gr._em_kline("100.510300", lmt=30)
        if closes:
            v = _amplitude_atr_pct(closes)
            if v and v > 0:
                return v
    except Exception as e:  # noqa: BLE001 — 振幅只是辅助展示, 失败用经验值
        logger.info("盘前简报: 预计振幅取数失败 (%s)", e)
    return None


def premarket_snapshot(*, allow_fetch: bool = True, use_cache: bool = True) -> dict:
    """盘前简报快照。

    Args:
        allow_fetch: False 时不联网, 直接返回 empty 结构 (测试/离线用)。
        use_cache: False 时强制重算。

    任何网络异常都返回 ok=False 的字典, 不抛异常。
    """
    key = f"premarket|{int(bool(allow_fetch))}"
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < _TTL:
            return hit[1]
    try:
        payload = _build(allow_fetch)
    except Exception as e:  # noqa: BLE001 — 简报整体失败必须降级
        logger.warning("盘前简报组装失败: %s", e)
        payload = {
            "ok": False,
            "bias": "未知",
            "bias_key": "flat",
            "strength": "未知",
            "strength_key": "weak",
            "probs": {"up": 33.3, "flat": 33.3, "down": 33.3},
            "gap_pct": {"low": 0.0, "high": 0.0},
            "amplitude": AMPL_FALLBACK,
            "signal": 0.0,
            "a50": {"price": None, "chg_pct": None, "as_of": None},
            "us": {"chg_pct": None, "as_of": None},
            "asia": {"chg_pct": None, "as_of": None},
            "cnh": {"price": None, "chg_pct": None, "note": "", "as_of": None},
            "drivers": [],
            "advice": f"盘前简报组装失败 ({type(e).__name__})。",
            "as_of": None,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = (now, payload)
    return payload


def invalidate_cache() -> None:
    """清空盘前简报缓存。"""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "AMPL_FALLBACK",
    "ASIA_WEIGHTS",
    "BIAS_TH",
    "CHG_CAP",
    "FLAT_BAND",
    "GAP_BETA",
    "GAP_SIGMA",
    "MID_TH",
    "STRONG_TH",
    "S_CAP",
    "US_WEIGHTS",
    "W_A50",
    "W_ASIA_BG",
    "W_ASIA_ONLY",
    "W_US_BG",
    "W_US_ONLY",
    "bias_of",
    "gap_probs",
    "invalidate_cache",
    "premarket_snapshot",
    "strength_of",
]
