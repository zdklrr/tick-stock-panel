"""交易计划 (plan) 纯函数模块 — 把「策略选股」翻译成「可执行的下单计划」。

职责边界: 本模块只做数学与口径 (止损/止盈/仓位/大盘缩放), 不碰磁盘、不写订单;
落盘与撮合在 app/strategy/paper_plans.py, HTTP 在 app/api/paper.py。

口径设计 (回答用户「更接近真人操作」):
  - 买入不是一个价, 而是一个区间: entry_ref ± entry_zone_pct% → entry_low/entry_high;
  - 止损先用波动率: stop = entry - atr_stop_mult * ATR, 有有效前低 (swing_low)
    且距离落在 [1.0, atr_stop_mult] * ATR 时改用前低 (结构位优先);
  - 仓位由风险预算反推: pos_pct = risk_budget_pct / stop_pct * 100, 即「止损被
    打掉时正好亏掉权益的 risk_budget_pct%」, 再夹到 [min_pos_pct, max_pos_pct];
  - 大盘情绪 (regime) 只缩放「仓位」与「止损松紧」, 不缩放止盈: 强势 1.2 倍仓位 +
    止损不收紧, 弱势 0.3 倍仓位 + 止损收紧到 0.6 倍。卖出触发逻辑不受择时影响
    (择时只影响开仓与初始止损, 已经持仓的保护不打折);
  - 缺 regime 表时一律按「无择时」执行 (倍率 1.0), 绝不抛异常;
  - 真到下单那一刻还会再做一次「动态仓位系数」(见 position_adjust): 建计划时的
    倍率只是冻结快照, 执行时用「当前大盘 / 建计划时大盘」的比值修正, 再乘上风险
    雷达的外围与回撤系数 —— 大盘转好加仓、转差缩量, 而不是弱势就干脆不买。

金额约定: gain_at_t1 / gain_at_t2 / loss_at_stop / risk_amount 一律为正数
(与 zdk 策略口径一致, 前端自己加 +- 号), rr = 目标1 的盈亏比。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

#: 交易计划买入执行方式
#:   zone      = 价格进入 entry_low~entry_high 区间即买 (默认, 最贴近真人挂单)
#:   next_open = 不等区间, 直接挂次日开盘单 (次日 09:30 开盘价成交)
#:   market    = 不等区间, 立即按当前价市价买入
ENTRY_MODES: tuple[str, ...] = ("zone", "next_open", "market")
DEFAULT_ENTRY_MODE = "zone"
#: 前端下拉/说明文案
ENTRY_MODE_LABELS: dict[str, str] = {
    "zone": "按买入区间 (价格进区间才买)",
    "next_open": "次日开盘 (不等区间, 开盘价成交)",
    "market": "立即市价 (不等区间, 当前价成交)",
}

#: 计划默认参数 (可按账户覆盖, 见 paper_plans.load_cfg)
DEFAULT_CFG: dict[str, float | int | bool | str] = {
    "risk_budget_pct": 1.0,        # 单笔最大亏损占权益%
    "max_pos_pct": 20.0,           # 单票仓位上限%
    "min_pos_pct": 2.0,            # 低于此不开计划
    "atr_stop_mult": 2.0,          # 止损 = entry - k*ATR
    "max_stop_pct": 12.0,          # 止损距离上限 (相对 entry)
    "entry_zone_pct": 2.0,         # 买入区间 = 现价 ±2%
    "target1_r": 1.5,              # 目标1 = entry + 1.5R
    "target2_r": 3.0,              # 目标2 = entry + 3R
    "max_hold_days": 20,           # 计划挂满 N 个自然日未触发即过期
    "max_symbols": 0,              # 最多同时持仓标的数 (0=不限制, 按风险自然收敛)
    "min_holdings": 1,             # 只数缩放的下限 (再悲观也至少留这么多只)
    "top_n": 10,                   # 单次最多生成多少只计划 (优中选优的默认截断)
    "max_total_exposure_pct": 100.0,  # 总仓位上限% (100=不额外收紧)
    "regime_scale_enabled": True,  # 仓位是否随大盘情绪缩放
    "regime_stop_tighten_enabled": True,  # 止损是否随大盘情绪收紧
    "default_entry_mode": DEFAULT_ENTRY_MODE,  # 策略→计划默认执行方式 (zone/next_open/market)
}

#: 动态仓位系数的合法区间 (adj = plan_adjust × radar_adjust, 夹在这里)
ADJ_FLOOR = 0.2
ADJ_CAP = 3.0
#: 风险雷达系数的合法区间 (外围系数 × 账户回撤系数)
RADAR_FLOOR = 0.5
RADAR_CAP = 1.5

#: 大盘情绪 → 仓位倍率 (state 见 app/services/regime_builder.py)
EXPOSURE_BY_STATE: dict[str, float] = {
    "strong": 1.2,
    "lean_strong": 1.0,
    "range": 0.75,
    "lean_weak": 0.5,
    "weak": 0.3,
}

#: 大盘情绪 → 止损收紧系数 (越小止损越近, 风险越小)
STOP_TIGHTEN_K: dict[str, float] = {
    "strong": 1.0,
    "lean_strong": 1.0,
    "range": 0.9,
    "lean_weak": 0.75,
    "weak": 0.6,
}

#: 参数合法区间 (越界值忽略并回落到默认值, 不让脏配置炸掉计划)
_CFG_RANGES: dict[str, tuple[float, float]] = {
    "risk_budget_pct": (0.05, 10.0),
    "max_pos_pct": (0.5, 100.0),
    "min_pos_pct": (0.5, 100.0),
    "atr_stop_mult": (0.5, 8.0),
    "max_stop_pct": (1.0, 50.0),
    "entry_zone_pct": (0.0, 15.0),
    "target1_r": (0.5, 10.0),
    "target2_r": (0.5, 20.0),
    "max_hold_days": (1, 250),
    "max_symbols": (0, 200),
    "min_holdings": (1, 200),
    "top_n": (1, 200),
    "max_total_exposure_pct": (5.0, 100.0),
}

_BOOL_KEYS = ("regime_scale_enabled", "regime_stop_tighten_enabled")
#: 取整的参数 (其余按 float 存)
_INT_KEYS = ("max_hold_days", "max_symbols", "min_holdings", "top_n")
#: 字符串枚举参数
_STR_KEYS = ("default_entry_mode",)


# ── 参数 ────────────────────────────────────────────────
def _num(value) -> float | None:
    """宽松转 float: 非数值 / NaN / inf 一律 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(value) -> float | None:
    v = _num(value)
    return v if v is not None and v > 0 else None


def normalize_entry_mode(value, default: str | None = DEFAULT_ENTRY_MODE) -> str | None:
    """买入执行方式归一化: 合法值原样返回, 缺失/非法返回 default。

    default 传 None 表示「调用方要自己判断合法性」(用于 API 拒绝非法值 vs
    配置项静默回落默认值这两种不同语义)。
    """
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in ENTRY_MODES:
            return mode
    return default


def merge_cfg(cfg: dict | None = None) -> dict:
    """DEFAULT_CFG ← cfg 合并 + 校验, 返回完整参数 (未知键丢弃, 非法值回落默认)。"""
    out = dict(DEFAULT_CFG)
    if isinstance(cfg, dict):
        for key, (lo, hi) in _CFG_RANGES.items():
            v = _num(cfg.get(key))
            if v is None or not (lo <= v <= hi):
                continue
            out[key] = int(v) if key in _INT_KEYS else v
        for key in _BOOL_KEYS:
            if key in cfg and cfg[key] is not None:
                out[key] = bool(cfg[key])
        for key in _STR_KEYS:
            mode = normalize_entry_mode(cfg.get(key), default=None)
            if mode is not None:
                out[key] = mode
    # 相对约束: min_pos_pct 不得高于 max_pos_pct; target2 必须大于 target1
    out["min_pos_pct"] = min(float(out["min_pos_pct"]), float(out["max_pos_pct"]))
    if not float(out["target2_r"]) > float(out["target1_r"]):
        out["target2_r"] = round(float(out["target1_r"]) * 2, 2)
    # 只数下限不能超过上限 (max_symbols=0 表示不限制, 此时下限保持原值)
    max_sym = int(out["max_symbols"])
    if max_sym > 0:
        out["min_holdings"] = max(1, min(int(out["min_holdings"]), max_sym))
    return out


def state_key(state) -> str | None:
    """接受 dict ({state,label,score}) 或裸字符串, 归一化为 regime state 名。"""
    raw = state.get("state") if isinstance(state, dict) else state
    if not isinstance(raw, str):
        return None
    key = raw.strip()
    return key if key in EXPOSURE_BY_STATE else None


def state_label(state) -> str | None:
    """state → 中文名 (缺表/未知状态返回 None)。"""
    key = state_key(state)
    if key is None:
        return None
    try:
        from app.services.regime_builder import STATE_LABELS
    except Exception:  # noqa: BLE001 — 情绪模块不可用时不影响计划
        return None
    return STATE_LABELS.get(key)


def exposure_multiplier(state, cfg: dict | None = None) -> float:
    """大盘情绪 → 仓位倍率; state 缺失或开关关闭 → 1.0 (无择时)。"""
    cfgd = merge_cfg(cfg)
    if not cfgd["regime_scale_enabled"]:
        return 1.0
    key = state_key(state)
    return EXPOSURE_BY_STATE.get(key, 1.0) if key else 1.0


def stop_tighten_k(state, cfg: dict | None = None) -> float:
    """大盘情绪 → 止损收紧系数; state 缺失或开关关闭 → 1.0 (不收紧)。"""
    cfgd = merge_cfg(cfg)
    if not cfgd["regime_stop_tighten_enabled"]:
        return 1.0
    key = state_key(state)
    return STOP_TIGHTEN_K.get(key, 1.0) if key else 1.0


def latest_state(data_dir: Path) -> dict | None:
    """从 regime_history 取最新一行 {date,state,label,score}; 缺表/异常一律 None。

    用户数据目录可能没有 regime_history/part.parquet (未开启情绪管道),
    这里必须优雅降级为「无择时」, 不能因为缺表让整个交易计划不可用。
    """
    try:
        from app.services.regime_builder import STATE_LABELS, load_regime_history

        df = load_regime_history(Path(data_dir))
        if df is None or df.is_empty() or "state" not in df.columns:
            return None
        ordered = df.sort("date") if "date" in df.columns else df
        rows = ordered.tail(1).to_dicts()
    except Exception as e:  # noqa: BLE001 — 数据缺失/parquet 损坏都按无择时处理
        logger.warning("trade_plan 读取大盘情绪失败 (按无择时执行): %s", e)
        return None
    if not rows:
        return None
    row = rows[0]
    key = state_key(row.get("state"))
    if key is None:
        return None
    day = row.get("date")
    score = _num(row.get("score"))
    return {
        "date": str(day) if day is not None else None,
        "state": key,
        "label": STATE_LABELS.get(key),
        "score": round(score, 2) if score is not None else None,
    }


def trend_exposure(data_dir: Path) -> dict:
    """大盘择时系数 (沪深300 收盘 vs MA20/MA60) → {state, label, exposure, ok}。

    直接复用 app/services/market_timing.py 的 trend_timing: 只读本地、不联网、
    取不到一律按**中性 1.0** (无择时), 绝不抛异常。刻意**不**用 synthesize() 的
    合成系数 —— 那个已经乘过外围风险与情绪周期, 拿来当「择时系数」会重复计入。

    数据缺失时 trend_timing 自己会给 unknown(0.5), 但「没测到」不等于「看空」:
    这里统一降级为 1.0, 与本模块其余地方「无择时 → 1.0」的口径一致, 避免用户
    刚装好软件、行情还没下全时计划只数被莫名其妙砍半。
    """
    out = {"state": None, "label": None, "exposure": 1.0, "ok": False}
    try:
        from app.services.market_timing import trend_timing

        snap = trend_timing(Path(data_dir)) or {}
        exposure = _pos(snap.get("exposure"))
        ok = bool(snap.get("ok"))
        out = {
            "state": snap.get("state"),
            "label": snap.get("state_label"),
            "exposure": round(exposure, 4) if (ok and exposure is not None) else 1.0,
            "ok": ok,
        }
    except Exception as e:  # noqa: BLE001 — 择时模块不可用按无择时处理
        logger.warning("trade_plan 读取大盘择时失败 (按 1.0 计): %s", e)
    return out


def effective_holdings_limit(
    cfg: dict | None = None,
    state=None,
    *,
    timing_exposure=None,
) -> dict:
    """只数上限随「大盘择时 × 情绪周期」缩放 → {limit, unlimited, ...}。

    limit = clamp(round(max_symbols × timing_exposure × phase_exposure),
                  min_holdings, max_symbols)

    - `max_symbols <= 0` 视为**不限制** (limit=None, unlimited=True), 此时不看系数;
    - `phase_exposure` 走 exposure_multiplier(state, cfg), 与建计划时的仓位倍率同源;
    - `timing_exposure` 由调用方从 trend_exposure() 传入 (缺省 1.0 = 不计择时);
    - 缩放只影响**买入只数上限**, 绝不参与卖出/止损 (卖出不打折, 见模块 docstring)。
    """
    cfgd = merge_cfg(cfg)
    max_symbols = int(cfgd["max_symbols"])
    min_holdings = max(1, int(cfgd["min_holdings"]))
    phase = round(exposure_multiplier(state, cfgd), 4)
    timing = _pos(timing_exposure)
    timing = round(timing, 4) if timing is not None else 1.0
    if max_symbols <= 0:
        return {
            "limit": None,
            "unlimited": True,
            "max_symbols": 0,
            "min_holdings": min_holdings,
            "timing_exposure": timing,
            "phase_exposure": phase,
            "raw": None,
            "scaled": False,
            "reason": "未设置只数上限 (max_symbols=0, 不限制)",
        }
    raw = max_symbols * timing * phase
    limit = round(raw)
    limit = min(max(limit, min_holdings), max_symbols)
    label = state_label(state) or "无择时"
    return {
        "limit": limit,
        "unlimited": False,
        "max_symbols": max_symbols,
        "min_holdings": min_holdings,
        "timing_exposure": timing,
        "phase_exposure": phase,
        "raw": round(raw, 2),
        "scaled": limit < max_symbols,
        "reason": (
            f"只数上限 {max_symbols} → 本期最多 {limit} 只"
            f" (大盘择时×{timing:.2f}、情绪{label}×{phase:.2f}; "
            f"下限 {min_holdings} 只, 由 min_holdings 控制)"
        ),
    }


# ── 动态仓位系数 ────────────────────────────────────────
def position_adjust(
    state_now=None,
    *,
    timing_scale=None,
    global_exposure=None,
    equity_factor=None,
    cfg: dict | None = None,
) -> dict:
    """下单时刻的动态仓位系数 (把「冻结的计划股数」修成「该买多少」)。

    两个独立来源相乘, 各自回答一个问题:
      - plan_adjust = 当前大盘倍率 / 建计划时冻结的倍率。建计划之后大盘转好就
        放大、转差就缩量; 除法的意义是「只算变化量」, 避免同一份大盘情绪在
        建计划与下单时各算一次 (那样强势市场永远追高、弱势市场永远买不到);
      - radar_adjust = 外围风险系数 × 账户回撤系数, 夹在 [0.5, 1.5]。**刻意不用
        风险雷达的合成 `exposure`**: 它已经乘过大盘系数, 会和 plan_adjust 重复
        计算; 而且它被夹在 [0.35, 1.10] 内, 除以大盘系数后在最需要缩仓的时候
        恰好恒等于 1, 反而把缩仓信号抹平。
    合起来的 adj 再夹在 [ADJ_FLOOR, ADJ_CAP], 防止脏数据把仓位放大到离谱。

    state_now/timing_scale 缺失, 或两个雷达系数都没给 → 对应部分按 1.0 (不加不减)。
    永远返回合法 dict, 不抛异常。
    """
    cfgd = merge_cfg(cfg)
    plan_adj = exposure_multiplier(state_now, cfgd)
    scale = _pos(timing_scale)
    # 建计划时的倍率缺失/为 0 → 认为当时是「无择时」, 只算当前值
    plan_adj = plan_adj / scale if scale else plan_adj

    gexp = _num(global_exposure)
    efac = _num(equity_factor)
    radar_adj = (gexp if gexp is not None else 1.0) * (efac if efac is not None else 1.0)
    radar_adj = min(max(radar_adj, RADAR_FLOOR), RADAR_CAP)

    adj = min(max(plan_adj * radar_adj, ADJ_FLOOR), ADJ_CAP)
    label = state_label(state_now) or "无择时"
    reason = (
        f"{label}×{plan_adj:.2f} 外围×{(gexp if gexp is not None else 1.0):.2f} "
        f"回撤×{(efac if efac is not None else 1.0):.2f} → {adj:.2f}"
    )
    return {
        "adj": round(adj, 4),
        "plan_adjust": round(plan_adj, 4),
        "radar_adjust": round(radar_adj, 4),
        "reason": reason,
    }


# ── 计划构造 ────────────────────────────────────────────
def build_plan(
    *,
    symbol: str,
    price,
    equity,
    name: str | None = None,
    atr=None,
    swing_low=None,
    cfg: dict | None = None,
    state=None,
    entry_mode: str | None = None,
) -> dict | None:
    """单只标的 → 交易计划; 参数非法或计划股数不足一手返回 None。

    price 是「入场参考价」(策略给出的最新收盘价), equity 是账户当前权益
    (现金+持仓市值)。所有价格字段四舍五入 2 位 (与 paper 域撮合价口径一致)。

    entry_mode: 买入执行方式 (zone/next_open/market); 缺省取 cfg.default_entry_mode。
    它只决定「什么时候买」, 不影响止损/止盈/仓位口径。
    """
    cfgd = merge_cfg(cfg)
    mode = normalize_entry_mode(entry_mode, default=cfgd["default_entry_mode"])
    sym = (symbol or "").strip()
    entry = _pos(price)
    eq = _pos(equity)
    if not sym or entry is None or eq is None:
        return None

    atr_v = _pos(atr)
    atr_fallback = atr_v is None
    if atr_v is None:
        atr_v = entry * 0.03          # 无 ATR 数据: 按 3% 波动率保守估计

    # ① 初始风险距离: 波动率止损, 前低有效时优先用结构位
    risk = float(cfgd["atr_stop_mult"]) * atr_v
    low = _pos(swing_low)
    if low is not None and low < entry:
        dist = entry - low
        if atr_v <= dist <= float(cfgd["atr_stop_mult"]) * atr_v:
            risk = dist

    # ② 大盘情绪收紧止损 (弱势市场把止损拉近, 缩短风险距离)
    risk *= stop_tighten_k(state, cfgd)

    # ③ 止损距离上限 (相对 entry)
    max_risk = entry * float(cfgd["max_stop_pct"]) / 100.0
    risk = min(risk, max_risk)
    stop = entry - risk
    # ④ 边界夹取: 止损不能低于 entry*0.5, 也不能贴到 entry 之上
    floor_stop = entry * 0.5
    ceil_stop = entry * 0.999
    if stop < floor_stop:
        stop = floor_stop
    if stop > ceil_stop:
        stop = ceil_stop
    stop = round(stop, 2)
    stop_pct = (entry - stop) / entry * 100.0
    if stop_pct <= 0:
        return None

    # ⑤ 仓位: 风险预算反推, 再夹上限
    raw_pos_pct = float(cfgd["risk_budget_pct"]) / stop_pct * 100.0
    capped = raw_pos_pct > float(cfgd["max_pos_pct"])
    pos_pct = min(max(raw_pos_pct, float(cfgd["min_pos_pct"])), float(cfgd["max_pos_pct"]))
    exposure = exposure_multiplier(state, cfgd)
    pos_pct = round(pos_pct * exposure, 2)
    if pos_pct <= 0:
        return None

    qty = int(eq * pos_pct / 100.0 / entry // 100) * 100
    if qty < 100:
        return None

    zone = float(cfgd["entry_zone_pct"])
    entry_low = round(entry * (1 - zone / 100.0), 2)
    entry_high = round(entry * (1 + zone / 100.0), 2)
    if entry_low <= 0:
        entry_low = round(entry, 2)
    if entry_high < entry_low:
        entry_low, entry_high = entry_high, entry_low

    risk_amount = round((entry - stop) * qty, 2)
    t1_r = float(cfgd["target1_r"])
    t2_r = float(cfgd["target2_r"])
    return {
        "symbol": sym,
        "name": name or None,
        "entry_ref": round(entry, 2),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "stop_pct": round(stop_pct, 2),
        "target1": round(entry + t1_r * (entry - stop), 2),
        "target2": round(entry + t2_r * (entry - stop), 2),
        "atr": round(atr_v, 4),
        "atr_pct": round(atr_v / entry * 100.0, 2),
        "atr_estimated": atr_fallback,
        "pos_pct": pos_pct,
        "qty_planned": qty,
        "risk_amount": risk_amount,
        "gain_at_t1": round(risk_amount * t1_r, 2),
        "gain_at_t2": round(risk_amount * t2_r, 2),
        "loss_at_stop": risk_amount,
        "rr": round(t1_r, 2),
        "state": state_key(state),
        "exposure": round(exposure, 2),
        "capped": capped,
        # 买入执行方式: zone=进区间才买 / next_open=次日开盘 / market=立即市价
        "entry_mode": mode,
        # 建计划时冻结的择时快照: 下单时用 position_adjust 与「当前」值作比
        "regime_label": state_label(state),
        "timing_scale": round(exposure, 4),
    }


def allocate_portfolio(
    plans: list[dict],
    *,
    equity,
    cfg: dict | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """组合仓位分配: 总仓位超上限时等比缩仓并整手重算股数。

    返回 (保留的计划, 被剔除的 [{symbol, reason}], 汇总)。

    为什么要做这一步: `build_plan` 逐只独立算 pos_pct (风险预算反推), 5 只各 20%
    就是 100%, 20 只会到 400% —— 组合层面根本买不起。这里在「生成计划之后」做一次
    组合约束: 总占比超过 `max_total_exposure_pct` 就按同一比例等比压缩每只, 再按压缩
    后的占比重算整手股数; 压到不足一手的那只直接剔除并给出原因 (前端展示 skipped)。

    只缩买入规模, 不碰止损/止盈比例与卖出逻辑 (卖出永不接受仓位系数影响)。
    """
    cfgd = merge_cfg(cfg)
    cap = float(cfgd["max_total_exposure_pct"])
    kept_inputs = [p for p in (plans or []) if isinstance(p, dict)]
    total = round(sum(float(p.get("pos_pct") or 0.0) for p in kept_inputs), 2)
    if total <= 0:
        return [], [], {
            "cap_pct": cap,
            "requested_pct": total,
            "total_pos_pct": 0.0,
            "scale": 1.0,
            "scaled": False,
        }
    scale = 1.0
    if total > cap > 0:
        scale = cap / total

    eq = _pos(equity) or 0.0
    kept: list[dict] = []
    dropped: list[dict] = []
    for plan in kept_inputs:
        plan = dict(plan)
        pos_pct = round(float(plan.get("pos_pct") or 0.0) * scale, 2)
        entry = _pos(plan.get("entry_ref"))
        qty = int(eq * pos_pct / 100.0 / entry // 100) * 100 if (eq > 0 and entry) else 0
        plan["pos_pct_scaled"] = scale < 1.0
        if qty < 100:
            dropped.append({
                "symbol": plan.get("symbol"),
                "reason": "组合仓位上限压缩后不足一手, 未生成计划",
            })
            continue
        plan["pos_pct"] = pos_pct
        plan["portfolio_pos_pct"] = pos_pct
        plan["qty_planned"] = qty
        stop = _pos(plan.get("stop"))
        if entry and stop is not None:
            risk_amount = round((entry - stop) * qty, 2)
            plan["risk_amount"] = risk_amount
            plan["loss_at_stop"] = risk_amount
            plan["gain_at_t1"] = round(risk_amount * float(cfgd["target1_r"]), 2)
            plan["gain_at_t2"] = round(risk_amount * float(cfgd["target2_r"]), 2)
        kept.append(plan)

    final_total = round(sum(float(p["pos_pct"]) for p in kept), 2)
    return kept, dropped, {
        "cap_pct": cap,
        "requested_pct": total,
        "total_pos_pct": final_total,
        "scale": round(scale, 4),
        "scaled": scale < 1.0,
    }


def _pick(row: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in row:
            return row[key]
    return None


def rank_rows(
    rows: list[dict],
    *,
    rank_key: str = "score",
    rank_desc: bool = True,
) -> list[dict]:
    """按评分列排序 (优中选优的第一步); 缺列/值不可比时保持原顺序。

    只对「有该列且能转成有限 float」的行排序, 其余行按原相对顺序排在后面 ——
    这样策略没给 score 列时行为与改造前完全一致 (不破坏既有调用方)。
    """
    seq = [r for r in (rows or []) if isinstance(r, dict)]
    key = (rank_key or "").strip()
    if not key or not all(key in r for r in seq):
        return seq
    values = [_num(r.get(key)) for r in seq]
    if any(v is None for v in values):
        return seq
    order = sorted(range(len(seq)), key=lambda i: values[i], reverse=bool(rank_desc))
    return [seq[i] for i in order]


def build_plans_for_rows(
    rows: list[dict],
    *,
    equity,
    cfg: dict | None = None,
    state=None,
    price_key: str = "close",
    atr_key: str = "atr_14",
    low_key: str = "low",
    name_key: str = "name",
    symbol_key: str = "symbol",
    entry_mode: str | None = None,
    rank_key: str = "score",
    rank_desc: bool = True,
) -> list[dict]:
    """策略结果行 (list[dict]) → 计划列表; 逐行构造, None 过滤, 同 symbol 去重。

    列名容错: 策略上下文不一定有 atr_14, 缺失时 build_plan 用 3% 波动率兜底;
    price/low 也按常见别名回退 (close → raw_close → price)。
    entry_mode 缺省取 cfg.default_entry_mode (账户级配置, 默认 zone)。
    先按 rank_key 降序排 (优中选优), 再逐行构造。
    """
    cfgd = merge_cfg(cfg)
    mode = normalize_entry_mode(entry_mode, default=cfgd["default_entry_mode"])
    out: list[dict] = []
    seen: set[str] = set()
    for row in rank_rows(list(rows or []), rank_key=rank_key, rank_desc=rank_desc):
        if not isinstance(row, dict):
            continue
        sym = row.get(symbol_key)
        if not isinstance(sym, str) or not sym.strip():
            continue
        if sym in seen:
            continue
        plan = build_plan(
            symbol=sym,
            name=row.get(name_key),
            price=_pick(row, (price_key, "close", "raw_close", "price", "entry_ref")),
            atr=_pick(row, (atr_key, "atr", "atr14")),
            swing_low=_pick(row, (low_key, "low", "raw_low")),
            equity=equity,
            cfg=cfg,
            state=state,
            entry_mode=mode,
        )
        if plan is None:
            continue
        seen.add(sym)
        out.append(plan)
    return out


# ── 预览 (只算不落盘) ─────────────────────────────────────
def preview_plan(
    *,
    symbol: str,
    equity,
    name: str | None = None,
    price=None,
    atr=None,
    swing_low=None,
    cfg: dict | None = None,
    state=None,
    entry_mode: str | None = None,
) -> dict:
    """单只标的 → 计划预览 (与 build_plan 同口径, 但**永不返回 None**)。

    build_plan 用 None 表示「这条做不出来」, 调用方只知道失败、不知道为什么失败 ——
    前端因此把所有列都画成 `--`, 用户完全看不懂哪里不对。preview_plan 把失败原因
    拆成可读的句子, 并在能算出部分结果时 (止损、买入区间) 照样返回, 让界面可以
    「先给区间和止损, 再说明为什么买不了」。

    返回字段:
      - `ok`: bool, 这只标的能不能生成计划;
      - `reason`: ok=False 时的原因 (中文, 直接给用户看);
      - `note`:  ok=True  时的补充说明 (如「无 ATR 数据, 按 3% 波动率估算」);
      - 其余为计划字段 (entry_low/entry_high/stop/target1/target2/pos_pct/qty_planned …),
        算不出来的字段为 None, 不再是 0。

    **绝不抛异常**: 参数缺失/价格无效等一律降级成 ok=False + 原因。
    """
    cfgd = merge_cfg(cfg)
    mode = normalize_entry_mode(entry_mode, default=cfgd["default_entry_mode"])
    sym = (symbol or "").strip()
    out: dict = {
        "symbol": sym,
        "name": name or None,
        "entry_ref": None,
        "entry_low": None,
        "entry_high": None,
        "stop": None,
        "stop_pct": None,
        "target1": None,
        "target2": None,
        "atr": None,
        "atr_pct": None,
        "atr_estimated": False,
        "pos_pct": None,
        "qty_planned": None,
        "risk_amount": None,
        "gain_at_t1": None,
        "gain_at_t2": None,
        "rr": None,
        "entry_mode": mode,
        "state": state_key(state),
        "exposure": None,
        "capped": False,
        "ok": False,
        "reason": "",
        "note": "",
    }
    if not sym:
        out["reason"] = "缺少股票代码"
        return out

    entry = _pos(price)
    if entry is None:
        out["reason"] = "缺少有效价格 (策略结果里没有 close / raw_close)"
        return out

    eq = _pos(equity)
    if eq is None:
        out["reason"] = "账户权益为 0 或尚未初始化, 先给账户入金"
        return out

    atr_v = _pos(atr)
    atr_estimated = atr_v is None
    if atr_v is None:
        atr_v = entry * 0.03

    # 止损: 与 build_plan 完全同一条计算路径
    risk = float(cfgd["atr_stop_mult"]) * atr_v
    low = _pos(swing_low)
    if low is not None and low < entry:
        dist = entry - low
        if atr_v <= dist <= float(cfgd["atr_stop_mult"]) * atr_v:
            risk = dist
    risk *= stop_tighten_k(state, cfgd)
    max_risk = entry * float(cfgd["max_stop_pct"]) / 100.0
    risk = min(risk, max_risk)
    stop = entry - risk
    stop = min(max(stop, entry * 0.5), entry * 0.999)
    stop = round(stop, 2)
    stop_pct = (entry - stop) / entry * 100.0

    # 区间与目标: 止损无效时也能给出区间, 只是不能下单
    zone = float(cfgd["entry_zone_pct"])
    entry_low = round(entry * (1 - zone / 100.0), 2)
    entry_high = round(entry * (1 + zone / 100.0), 2)
    if entry_low <= 0:
        entry_low = round(entry, 2)
    if entry_high < entry_low:
        entry_low, entry_high = entry_high, entry_low

    out.update({
        "entry_ref": round(entry, 2),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "stop_pct": round(stop_pct, 2),
        "atr": round(atr_v, 4),
        "atr_pct": round(atr_v / entry * 100.0, 2),
        "atr_estimated": atr_estimated,
        "exposure": round(exposure_multiplier(state, cfgd), 2),
    })
    if atr_estimated:
        out["note"] = "无 ATR 数据, 按 3% 波动率估算止损"

    if stop_pct <= 0:
        out["reason"] = "止损距离无效 (止损价不低于现价), 请检查计划参数"
        return out

    # 仓位: 风险预算反推 + 夹取
    raw_pos_pct = float(cfgd["risk_budget_pct"]) / stop_pct * 100.0
    capped = raw_pos_pct > float(cfgd["max_pos_pct"])
    pos_pct = min(max(raw_pos_pct, float(cfgd["min_pos_pct"])), float(cfgd["max_pos_pct"]))
    exposure = exposure_multiplier(state, cfgd)
    pos_pct = round(pos_pct * exposure, 2)
    out["pos_pct"] = pos_pct
    out["capped"] = capped

    t1_r = float(cfgd["target1_r"])
    t2_r = float(cfgd["target2_r"])
    out["target1"] = round(entry + t1_r * (entry - stop), 2)
    out["target2"] = round(entry + t2_r * (entry - stop), 2)
    out["rr"] = round(t1_r, 2)

    if pos_pct <= 0:
        out["reason"] = "算出的仓位为 0, 请检查仓位参数或大盘情绪系数"
        return out

    qty = int(eq * pos_pct / 100.0 / entry // 100) * 100
    out["qty_planned"] = qty
    if qty < 100:
        need = round(entry * 100, 2)
        out["reason"] = (
            f"计划股数不足一手 (按仓位 {pos_pct}% 只能买 {qty} 股), "
            f"一手 {need} 元; 提高仓位上限、换成低价标的, 或给账户加资金"
        )
        return out

    risk_amount = round((entry - stop) * qty, 2)
    out.update({
        "qty_planned": qty,
        "risk_amount": risk_amount,
        "gain_at_t1": round(risk_amount * t1_r, 2),
        "gain_at_t2": round(risk_amount * t2_r, 2),
        "ok": True,
        "reason": "",
    })
    return out


def preview_plans_for_rows(
    rows: list[dict],
    *,
    equity,
    cfg: dict | None = None,
    state=None,
    entry_mode: str | None = None,
    price_key: str = "close",
    atr_key: str = "atr_14",
    low_key: str = "low",
    name_key: str = "name",
    symbol_key: str = "symbol",
) -> list[dict]:
    """逐行预览 (与 build_plans_for_rows 同样的列名容错), **保持传入顺序**。

    与 build_plans_for_rows 不同: 不过滤失败行, 也不去重 —— 预览的目的就是
    把每一行都讲清楚 (哪些能做、哪些不能、为什么)。调用方自己去重/排序。
    """
    out: list[dict] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        sym = row.get(symbol_key)
        if not isinstance(sym, str) or not sym.strip():
            continue
        out.append(preview_plan(
            symbol=sym,
            name=row.get(name_key),
            price=_pick(row, (price_key, "close", "raw_close", "price", "entry_ref")),
            atr=_pick(row, (atr_key, "atr", "atr14")),
            swing_low=_pick(row, (low_key, "low", "raw_low")),
            equity=equity,
            cfg=cfg,
            state=state,
            entry_mode=entry_mode,
        ))
    return out
