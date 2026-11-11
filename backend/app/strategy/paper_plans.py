"""模拟盘交易计划 (plan) 领域模块 — 用策略给出的区间/止损/止盈自动下单。

账务边界: 本模块不自己记账, 一切买卖都走 paper.create_order / paper 域撮合,
计划只是「意图 + 触发条件」的状态机, 落盘在
data/paper/accounts/{account_id}/plans/plan_*.json。

状态机:
  pending ──价格进入买入区间──> bought ──成交入持仓──> holding ──> closed
     │                            │                        │
     ├─超过 max_hold_days──────> expired                   └─清仓成交后由持仓消失判定
     └─手动取消───────────────> cancelled

触发口径 (每一轮行情快照调用一次 evaluate_plans):
  - pending: 按计划自己的 entry_mode 决定怎么买 (见下), source="plan:{id}" 便于回溯;
  - holding/bought: 现价 ≤ stop 或 ≤ trail_stop → 清仓; ≥ target2 → 清仓;
    ≥ target1 且未减半 → 卖出半仓并把 trail_stop 抬到保本价 (t1_done=True);
  - 卖出永远受 T+1 可卖数量约束 (paper.sell_capacity), 当日买入当日不卖;
  - 已挂出未成交的同向计划单不重复挂 (避免每次轮询刷单)。

买入执行方式 (entry_mode, 老计划字段缺失一律按 zone):
  - zone      : 现价落在 [entry_low, entry_high] 内才买 → 挂市价单当轮成交 (默认);
  - market    : 不等区间, 第一轮快照就按现价挂市价单;
  - next_open : 不等区间, 挂次日开盘单 (order_type=next_open), 由盘后 settle_day
                按次日 raw_open 成交 —— 用户明确要求「不要非等到次日开盘」时改 zone。

仓位不再是「建计划时算死的股数」, 而是下单那一刻按 `plan_adjust × radar_adjust`
动态折算 (见 trade_plan.position_adjust):
  - plan_adjust = 当前大盘倍率 / 建计划时冻结的倍率 (转好加仓、转差缩量);
  - radar_adjust = 风险雷达的外围系数 × 账户回撤系数, 夹在 [0.5, 1.5];
  - 合成系数夹在 [0.2, 3.0], 缩到不足一手时买最小一手, 现金/总仓位上限不够才放弃;
  - 人为闸门默认全关 (max_symbols=0 / max_total_exposure_pct=100), 仓位交给风险决定。

与大盘情绪的关系: 情绪只影响「开仓数量」, 不影响本模块的卖出触发 —— 卖出保护
不打折。止损/移动止损/目标1/目标2 的触发条件与数量不受任何系数影响, 系数最多
出现在事件消息里做展示。这是铁律。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date
from pathlib import Path

from app.market_time import cn_now, cn_today
from app.services.fs_utils import atomic_write_text
from app.strategy import paper, trade_plan

logger = logging.getLogger(__name__)

PLAN_STATUSES = ("pending", "bought", "holding", "closed", "expired", "cancelled")
#: 视为「已建仓」的状态 (参与止损/止盈判定)
_LIVE_STATUSES = ("bought", "holding")
#: 视为「活跃」的状态 (参与列表默认过滤 / 同标的去重)
_ACTIVE_STATUSES = ("pending", "bought", "holding")

#: 风险雷达系数的本地缓存秒数 —— evaluate_plans 由行情轮询驱动 (每轮几秒一次),
#: 每次都联网取外围行情会拖慢主链路, 60 秒内复用同一份系数足够。
_RADAR_TTL = 60.0
#: {(data_dir, account_id): (取到时的 monotonic, {global_exposure, equity_factor})}
_RADAR_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_RADAR_LOCK = threading.Lock()
#: 计划里保留的动态系数历史条数
_ADJ_HISTORY_MAX = 20


# ── 路径与读写 ──────────────────────────────────────────
def plans_dir(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> Path:
    d = paper._root(data_dir, account_id) / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cfg_path(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> Path:
    return paper._root(data_dir, account_id) / "plan_config.json"


def _now_iso() -> str:
    return cn_now().isoformat()


def _norm_status_filter(status) -> set[str] | None:
    """status 过滤归一化: str(逗号分隔) / set / list / None → set|None。"""
    if status is None:
        return None
    if isinstance(status, str):
        items = {s.strip() for s in status.split(",")}
    elif isinstance(status, (set, frozenset, list, tuple)):
        items = {str(s).strip() for s in status}
    else:
        return None
    items.discard("")
    return items or None


def _normalize_plan(plan: dict) -> dict:
    """读盘后补齐默认字段 (旧版本/手工编辑的文件不至于让调用方 KeyError)。"""
    out = dict(plan)
    out.setdefault("name", None)
    out.setdefault("note", None)
    out.setdefault("source", None)
    out.setdefault("last_error", None)
    out.setdefault("updated_at", None)
    for key in (
        "entry_ref", "stop", "stop_pct", "target1", "target2", "trail_stop",
        "atr", "atr_pct", "pos_pct", "risk_amount", "gain_at_t1", "gain_at_t2",
        "loss_at_stop", "rr", "exposure", "entry_price",
    ):
        out.setdefault(key, None)
    out.setdefault("qty_planned", 0)
    out.setdefault("qty_filled", 0)
    out.setdefault("entry_date", None)
    out.setdefault("state", None)
    out.setdefault("capped", False)
    out.setdefault("t1_done", False)
    # 组合仓位分配留下的痕迹 (手工计划/老计划没有这两个键)
    out.setdefault("pos_pct_scaled", False)
    out.setdefault("portfolio_pos_pct", None)
    # 老计划文件没有 entry_mode → 视为 zone (即原来的「进区间才买」行为)
    out["entry_mode"] = trade_plan.normalize_entry_mode(
        out.get("entry_mode"), default=trade_plan.DEFAULT_ENTRY_MODE
    )
    # 动态仓位系数的展示与留痕 (旧计划文件没有这些键)
    out.setdefault("regime_label", None)
    out.setdefault("timing_scale", None)
    out.setdefault("last_adj", None)
    out.setdefault("last_adj_reason", None)
    out.setdefault("adj_history", [])
    if out.get("status") not in PLAN_STATUSES:
        out["status"] = "pending"
    return out


def list_plans(
    data_dir: Path,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    status=None,
) -> list[dict]:
    """账户全部计划 (created_at 倒序); status 可传 str/set 做过滤。"""
    wanted = _norm_status_filter(status)
    out: list[dict] = []
    for f in sorted(plans_dir(data_dir, account_id).glob("plan_*.json")):
        try:
            plan = _normalize_plan(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001 — 单文件损坏不影响其它计划
            logger.warning("paper plan load failed %s: %s", f.name, e)
            continue
        if wanted is not None and plan.get("status") not in wanted:
            continue
        out.append(plan)
    out.sort(key=lambda p: (p.get("created_at") or "", p.get("id") or ""), reverse=True)
    return out


def get_plan(data_dir: Path, plan_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = plans_dir(data_dir, account_id) / f"{(plan_id or '').strip()}.json"
    if not p.name.startswith("plan_") or not p.exists():
        return None
    try:
        return _normalize_plan(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        logger.warning("paper plan load failed %s: %s", p.name, e)
        return None


def save_plan(data_dir: Path, plan: dict, account_id: str | None = None) -> dict:
    """落盘一份计划 (原子写), 返回写入内容。"""
    aid = account_id or plan.get("account_id") or paper.DEFAULT_ACCOUNT_ID
    plan["account_id"] = aid
    plan["updated_at"] = _now_iso()
    atomic_write_text(
        plans_dir(data_dir, aid) / f"{plan['id']}.json",
        json.dumps(plan, ensure_ascii=False, indent=2),
    )
    return plan


def delete_plan(data_dir: Path, plan_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    """删除计划文件 (物理删除, 不可恢复); 用于前端「删除」。"""
    p = plans_dir(data_dir, account_id) / f"{(plan_id or '').strip()}.json"
    if not p.name.startswith("plan_") or not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError as e:
        logger.warning("paper plan delete failed %s: %s", p.name, e)
        return False


def cancel_plan(data_dir: Path, plan_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    """把计划置为 cancelled (留档, 用于前端「取消」); 不可取消已了结的状态。"""
    with paper.PAPER_LOCK:
        plan = get_plan(data_dir, plan_id, account_id)
        if plan is None:
            return None
        if plan["status"] in ("closed", "expired", "cancelled"):
            return plan
        plan["status"] = "cancelled"
        plan["last_error"] = None
        return save_plan(data_dir, plan, account_id)


# ── 计划参数 (cfg) ──────────────────────────────────────
def load_cfg(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    """账户计划参数 (缺文件/损坏 → 默认参数; 越界值回落默认)。"""
    p = cfg_path(data_dir, account_id)
    if not p.exists():
        return trade_plan.merge_cfg(None)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("paper plan cfg load failed, 用默认参数: %s", e)
        return trade_plan.merge_cfg(None)
    cfg = raw.get("cfg") if isinstance(raw, dict) and "cfg" in raw else raw
    return trade_plan.merge_cfg(cfg if isinstance(cfg, dict) else None)


def save_cfg(data_dir: Path, cfg: dict | None, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    merged = trade_plan.merge_cfg(cfg)
    atomic_write_text(
        cfg_path(data_dir, account_id),
        json.dumps({"cfg": merged, "updated_at": _now_iso()}, ensure_ascii=False, indent=2),
    )
    return merged


def labels() -> dict:
    """计划面板用的枚举文案 (state → 中文 / 仓位倍率 / 止损收紧系数)。"""
    state_labels: dict[str, str] = {}
    try:
        from app.services.regime_builder import STATE_LABELS

        state_labels = dict(STATE_LABELS)
    except Exception:  # noqa: BLE001 — 情绪模块不可用时仍返回倍率表
        state_labels = {}
    return {
        "state_labels": {k: state_labels.get(k, k) for k in trade_plan.EXPOSURE_BY_STATE},
        "exposure": dict(trade_plan.EXPOSURE_BY_STATE),
        "stop_k": dict(trade_plan.STOP_TIGHTEN_K),
        "status_labels": {
            "pending": "待触发",
            "bought": "已买入",
            "holding": "持仓中",
            "closed": "已了结",
            "expired": "已过期",
            "cancelled": "已取消",
        },
        # 买入执行方式 (前端下拉)
        "entry_modes": list(trade_plan.ENTRY_MODES),
        "entry_mode_labels": dict(trade_plan.ENTRY_MODE_LABELS),
        "default_entry_mode": trade_plan.DEFAULT_ENTRY_MODE,
        # 人为闸门默认全关, 仓位交给策略与风险自然收敛
        "max_symbols": "最多持仓只数(0=不限)",
        "max_total_exposure_pct": "总仓位上限%",
    }


# ── 风险雷达 / 动态仓位系数 ─────────────────────────────
#: 雷达完全不可用时用的中性系数 (不加不减)
_NEUTRAL_RADAR: dict = {"ok": False, "global_exposure": 1.0, "equity_factor": 1.0}
#: 「没传这个参数」的哨兵 (区分「没传」与「传了 None=无择时」)
_MISSING = object()


def invalidate_radar_cache() -> None:
    """清空雷达系数缓存 (设置变更 / 手动刷新风险雷达后调用)。"""
    with _RADAR_LOCK:
        _RADAR_CACHE.clear()


def _radar_coeffs(data_dir: Path, account_id: str) -> dict:
    """风险雷达的两个系数 {ok, global_exposure, equity_factor}; 不可用一律 1.0。

    这里**必须能降级**: 雷达模块没部署 (ImportError)、雷达自报 ok=False、或取数
    抛异常 → 全部按 1.0 (无风险信号, 不加不减), 绝不能让模拟盘因为雷达不可用而
    停摆或抛异常。刻意取「外围系数 × 账户回撤系数」而不是合成 exposure —— 合成值
    已经乘过大盘情绪, 会和 plan_adjust 重复计算, 且被夹在 [0.35, 1.10] 内, 在最
    需要缩仓的时候恰好恒等于 1, 反而把缩仓信号抹掉。

    行情轮询每轮都会走到这里, 60 秒内复用同一份结果, 不重复联网取外围行情。
    """
    key = (str(data_dir), account_id)
    now = time.monotonic()
    with _RADAR_LOCK:
        hit = _RADAR_CACHE.get(key)
    if hit is not None and now - hit[0] < _RADAR_TTL:
        return dict(hit[1])

    out = dict(_NEUTRAL_RADAR)
    try:
        from app.services import global_risk
    except ImportError:  # 雷达模块未部署/被裁剪 → 按无风险信号
        logger.debug("risk radar 模块不可用, 仓位系数按 1.00")
    else:
        gexp: float | None = None
        efac: float | None = None
        try:
            fn = getattr(global_risk, "exposure_for_paper", None)
            if callable(fn):
                gexp, efac = fn(Path(data_dir), account_id)
            else:  # 契约退化: 只有 radar_snapshot
                snap = global_risk.radar_snapshot(Path(data_dir), account_id=account_id)
                if snap.get("ok"):
                    gexp = snap.get("global_exposure")
                    efac = (snap.get("equity") or {}).get("factor")
            g, e = _f(gexp), _f(efac)
            out = {
                "ok": g is not None and e is not None,
                "global_exposure": g if g is not None else 1.0,
                "equity_factor": e if e is not None else 1.0,
            }
        except Exception as e:  # noqa: BLE001 — 雷达取数失败按无风险信号降级
            logger.warning("risk radar 取数失败 (按无风险信号): %s", e)
    with _RADAR_LOCK:
        _RADAR_CACHE[key] = (now, dict(out))
    return dict(out)


def buy_ctx(
    data_dir: Path,
    account_id: str,
    *,
    cfg: dict | None = None,
    state_now=_MISSING,
    radar=_MISSING,
) -> dict:
    """下单时刻的上下文 {cfg, state, radar, cap_pct} (evaluate_plans 每轮算一次复用)。

    radar 传 dict 就直接用 (含中性系数), 传 _MISSING 才去取雷达 —— 没有待买入
    计划时调用方会传中性系数, 避免为了「看一眼持仓」白等一次联网取外围行情。
    在网络取数与读盘方面刻意不持锁: 雷达首次取数要几百毫秒, 不能卡在 PAPER_LOCK 里。

    0.3.48: cap_pct 是决策卡「今日上限」的买入硬闸门, 复用本轮已取好的
    state/radar, 只补一次只读本地的 trend_exposure, 零额外联网 (绝不调
    synthesize())。_try_buy 在 create_order 前用它判断「买完是否超限」。
    """
    rad = radar if isinstance(radar, dict) else _radar_coeffs(data_dir, account_id)
    state = trade_plan.latest_state(data_dir) if state_now is _MISSING else state_now
    return {
        "cfg": trade_plan.merge_cfg(cfg) if cfg is not None else load_cfg(data_dir, account_id),
        "state": state,
        "radar": rad,
        "cap_pct": (
            suggested_exposure_pct(data_dir, account_id, state=state, radar=rad)
            if SUGGESTED_CAP_ENABLED else 100.0
        ),
    }


def _plan_adj(plan: dict, ctx: dict) -> dict:
    """单个计划的动态仓位系数 (见 trade_plan.position_adjust)。"""
    radar = ctx.get("radar") or _NEUTRAL_RADAR
    return trade_plan.position_adjust(
        ctx.get("state"),
        timing_scale=plan.get("timing_scale"),
        global_exposure=radar.get("global_exposure"),
        equity_factor=radar.get("equity_factor"),
        cfg=ctx.get("cfg"),
    )


def _note_adj(plan: dict, info: dict) -> None:
    """把本次用的系数写进计划 (last_adj / last_adj_reason / adj_history 最近 20 条)。"""
    adj = float(info.get("adj") or 1.0)
    reason = str(info.get("reason") or "")
    plan["last_adj"] = round(adj, 4)
    plan["last_adj_reason"] = reason
    history = plan.get("adj_history")
    history = list(history) if isinstance(history, list) else []
    history.append({"ts": _now_iso(), "adj": round(adj, 4), "reason": reason})
    plan["adj_history"] = history[-_ADJ_HISTORY_MAX:]


def _affordable_qty(data_dir: Path, account_id: str, px: float, want: int) -> int:
    """现金约束: 把 want 压到真正买得起的整手股数 (按参考价 + 滑点 + 佣金估算)。"""
    acc = paper.get_account(data_dir, account_id)
    cash = float((acc or {}).get("cash") or 0.0)
    if want <= 0 or cash <= 0 or px <= 0:
        return 0
    slip = float((acc or {}).get("slippage_bps") or 0) / 10000.0
    comm = float((acc or {}).get("commission_pct") or 0.0)
    unit = px * (1 + slip)
    lots = int(cash // (unit * paper.LOT_SIZE))
    while lots > 0:
        cand = lots * paper.LOT_SIZE
        if cand * unit + paper.buy_fee(cand, unit, comm) <= cash:
            break
        lots -= 1
    return max(min(paper.normalize_qty(want), lots * paper.LOT_SIZE), 0)


#: 「今日上限 = 买入硬闸门」总开关 (0.3.48)。置 False 可回到旧行为
#: (只数/金额缩放 + cfg.max_total_exposure_pct), 改完需重新打包。
SUGGESTED_CAP_ENABLED = True
#: 合成仓位上下限, 与 market_timing.SYN_FLOOR / SYN_CAP 保持一致
_CAP_FLOOR = 0.15
_CAP_CAP = 1.20
#: trend_timing(_heal=False) 只读本地 parquet, 但 21 个账户 × 每轮行情快照全扫
#: 一遍分区在 NAS (J1900) 上仍嫌浪费: 按 data_dir 缓存 60 秒 (与雷达同量级)。
_TIMING_CACHE: dict[str, tuple[float, float]] = {}
_TIMING_CACHE_TTL = 60.0
_TIMING_LOCK = threading.Lock()


def _cap_timing(data_dir: Path) -> float:
    """只读本地的大盘择时系数 (60s 缓存, 数据判不出四态 → 1.0 中性)。"""
    key = str(data_dir)
    now = time.monotonic()
    with _TIMING_LOCK:
        hit = _TIMING_CACHE.get(key)
    if hit is not None and now - hit[0] < _TIMING_CACHE_TTL:
        return hit[1]
    from app.services import market_timing as _mt

    snap = _mt.trend_timing(Path(data_dir), _heal=False) or {}
    t = float(snap.get("exposure") or 1.0) if snap.get("ok") else 1.0
    with _TIMING_LOCK:
        _TIMING_CACHE[key] = (now, t)
    return t


def suggested_exposure_pct(
    data_dir: Path,
    account_id: str,
    *,
    timing: float | None = None,
    phase: float | None = None,
    risk: float | None = None,
    state=None,
    radar: dict | None = None,
) -> float:
    """今日上限 (%) = clamp(大盘择时 × 情绪周期 × 外围风险, 0.15, 1.20) × 100.

    与决策卡 `/api/risk/advice` 的 `suggested_exposure_pct` **数值同源**, 但
    **刻意不调 `synthesize()`** —— 那会额外跑一次带网络自愈的 `trend_timing()`
    和联网的 `radar_snapshot()`, 在买入热路径 (每轮行情快照 × 每个账户) 会造成
    NAS 上的同步等待 (0.3.44 曾因此让 /api/risk/advice 120s 超时)。三路系数全部
    允许调用方传入、复用已缓存的结果, 做到零额外网络:
      - timing: 传入则复用; 否则 `_cap_timing()` = `trend_timing(_heal=False)`
                只读本地 parquet (60s 缓存), 判不出四态按 1.0 中性 —— 与
                `synthesize()` 的 timing 部分严格同源, 但绝不触发网络自愈;
      - phase : 传入则复用; 否则 `trade_plan.exposure_multiplier(state)`
                (不传 cfg, 与 `market_timing._phase_part` 严格一致);
      - risk  : 传入则复用; 否则 `radar['global_exposure']` 或 `_radar_coeffs()`
                (== `radar_snapshot().global_exposure`, 不含账户回撤)。
    任何一路取数失败按「不限制」(100.0) 降级, 绝不抛异常阻断买入。
    """
    try:
        t = float(timing) if timing is not None else _cap_timing(Path(data_dir))
        p = (float(phase) if phase is not None
             else float(trade_plan.exposure_multiplier(
                 state if state is not None else trade_plan.latest_state(Path(data_dir)))))
        if risk is not None:
            r = float(risk)
        else:
            rad = radar if radar is not None else _radar_coeffs(Path(data_dir), account_id)
            r = float(rad.get("global_exposure") or 1.0)
    except Exception as e:  # noqa: BLE001 — 系数不可用按不限制处理
        logger.warning("suggested_exposure_pct 取数失败 (按不限制): %s", e)
        return 100.0
    raw = t * p * r
    return round(min(max(raw, _CAP_FLOOR), _CAP_CAP) * 100.0, 1)


def _would_exceed_exposure_cap(
    data_dir: Path,
    account_id: str,
    px: float,
    qty: int,
    cap_pct: float,
    price_map: dict[str, float] | None = None,
) -> bool:
    """买完这一笔后「持仓市值 / 总资产」是否会超过 cap_pct%.

    买入是现金 → 持仓, 总资产不变 (忽略手续费), 所以分子加 `qty*px`、分母不变。
    估值与决策卡「当前仓位」同源: 传 price_map (本轮实时快照) 就按实时价估,
    没有则退回最近日线收盘价 (`valuation="daily"`); 两者都纯本地读 parquet,
    30s 缓存, 绝不联网。取数失败一律放行 (宁可多买一手, 也不因展示层问题卡单)。
    """
    if cap_pct >= 100 or qty <= 0 or px <= 0:
        return False
    try:
        if price_map:
            ov = paper.overview(data_dir, price_map, account_id=account_id) or {}
        else:
            ov = paper.overview(data_dir, account_id=account_id, valuation="daily") or {}
        equity = float(ov.get("total") or 0.0)
        mv = float(ov.get("market_value") or 0.0)
        if equity <= 0:
            return False
        return (mv + qty * px) / equity > cap_pct / 100.0
    except Exception as e:  # noqa: BLE001
        logger.warning("exposure cap 估值失败 (放行): %s", e)
        return False


def _exposure_capped_qty(
    data_dir: Path, account_id: str, px: float, want: int, pct: float
) -> int:
    """总仓位上限约束: 买完之后持仓市值 / 权益 不超过 pct%; pct>=100 视为不限制。"""
    if want <= 0 or pct >= 100:
        return want
    ov = paper.overview(data_dir, account_id=account_id) or {}
    equity = float(ov.get("total") or 0.0)
    mv = float(ov.get("market_value") or 0.0)
    if equity <= 0 or px <= 0:
        return want
    room = equity * pct / 100.0 - mv
    if room <= 0:
        return 0
    lots = int(room // (px * paper.LOT_SIZE))
    return max(min(paper.normalize_qty(want), lots * paper.LOT_SIZE), 0)


def _sized_qty(
    data_dir: Path,
    account_id: str,
    plan: dict,
    px: float,
    base: int,
    adj: float,
    cfg: dict,
) -> tuple[int, str | None]:
    """qty_planned × adj → 真正下单的整手股数; 返回 (股数, 放弃原因)。

    顺序: 折算 → 不足一手补最小一手 → 现金约束 → 总仓位上限约束。
    「缩到 0 才放弃」: 只要还算得出一手且现金够就买最小一手, 弱势市场也保留
    参与度 (大盘转好时才有底仓可以加)。
    """
    want = paper.normalize_qty(round(base * max(adj, 0.0)))
    if want < paper.LOT_SIZE:
        # adj 很小或 base 本身不足一手: 只要系数没被压到 0, 就买最小一手
        want = paper.LOT_SIZE if adj > 0 else 0
    if want <= 0:
        return 0, f"仓位系数 {adj:.2f} 把计划仓位压到 0, 本次不买入"
    want = _affordable_qty(data_dir, account_id, px, want)
    if want <= 0:
        return 0, "可用资金不足一手, 本次不买入"
    capped = _exposure_capped_qty(
        data_dir, account_id, px, want, float(cfg.get("max_total_exposure_pct") or 100.0)
    )
    if capped <= 0:
        return 0, f"已达总仓位上限 {float(cfg.get('max_total_exposure_pct')):.0f}%, 本次不买入"
    return capped, None


# ── 建/改计划 ───────────────────────────────────────────
def _f(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def create_plan(data_dir: Path, payload: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    """新建手工计划 (payload 来自 API body); 参数非法抛 ValueError。

    stop/target1/target2 可缺省 (由区间中枢与 cfg 推算), pos_pct/qty_planned 二选一,
    都没给时按账户 cfg 的仓位上限折算 (拿不到权益则只记 1 手)。
    entry_mode 缺省取账户 cfg 的 default_entry_mode; 显式传入的值非法会抛 ValueError
    (手工建计划时静默回落容易让人以为设置生效了)。
    """
    if not isinstance(payload, dict):
        raise ValueError("计划参数必须是对象")
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol 不能为空")
    low = _f(payload.get("entry_low"))
    high = _f(payload.get("entry_high"))
    if low is None or high is None or low <= 0 or high <= 0:
        raise ValueError("买入区间 entry_low / entry_high 必须是正数")
    if low > high:
        low, high = high, low
    stop = _f(payload.get("stop"))
    if stop is not None and not (0 < stop < high):
        raise ValueError("止损价必须为正数且低于买入区间上沿")

    entry_ref = _f(payload.get("entry_ref")) or round((low + high) / 2, 2)
    cfg = load_cfg(data_dir, account_id)
    raw_mode = payload.get("entry_mode")
    if raw_mode is None or (isinstance(raw_mode, str) and not raw_mode.strip()):
        mode = trade_plan.normalize_entry_mode(None, default=cfg["default_entry_mode"])
    else:
        mode = trade_plan.normalize_entry_mode(raw_mode, default=None)
        if mode is None:
            raise ValueError(
                f"entry_mode 只能是 {'/'.join(trade_plan.ENTRY_MODES)}, 收到 {raw_mode!r}"
            )
    pos_pct = _f(payload.get("pos_pct"))
    if pos_pct is not None:
        pos_pct = min(max(pos_pct, 0.0), 100.0)
    qty_planned = payload.get("qty_planned")
    try:
        qty_planned = int(qty_planned) if qty_planned is not None else None
    except (TypeError, ValueError):
        qty_planned = None
    if qty_planned is not None:
        qty_planned = paper.normalize_qty(max(qty_planned, 0))
    equity = _f((paper.overview(data_dir, account_id=account_id) or {}).get("total"))
    if pos_pct is None and qty_planned:
        pos_pct = round(qty_planned * entry_ref / equity * 100, 2) if equity else None
    if pos_pct is None:
        pos_pct = float(cfg["max_pos_pct"])
    if not qty_planned:
        if equity:
            qty_planned = int(equity * float(pos_pct) / 100.0 / entry_ref // 100) * 100
        qty_planned = max(qty_planned or 0, 100 if equity is None else 0)
    if not qty_planned or qty_planned < 100:
        raise ValueError("计划股数不足一手 (100 股): 提高仓位占比或本金")

    stop_pct = round((entry_ref - stop) / entry_ref * 100, 2) if stop else None
    t1_r = float(cfg["target1_r"])
    t2_r = float(cfg["target2_r"])
    target1 = _f(payload.get("target1"))
    target2 = _f(payload.get("target2"))
    if stop is not None:
        target1 = target1 or round(entry_ref + t1_r * (entry_ref - stop), 2)
        target2 = target2 or round(entry_ref + t2_r * (entry_ref - stop), 2)
    # 建计划当天冻结择时快照: 下单时用「当前倍率 / 这个快照」算加减仓幅度
    state_now = trade_plan.latest_state(data_dir)
    timing_scale = round(trade_plan.exposure_multiplier(state_now, cfg), 4)

    plan = {
        "id": paper._new_id("plan"),
        "account_id": account_id,
        "symbol": symbol,
        "name": (str(payload.get("name") or "").strip() or None),
        "status": "pending",
        "entry_ref": round(entry_ref, 2),
        "entry_low": round(low, 2),
        "entry_high": round(high, 2),
        "stop": round(stop, 2) if stop is not None else None,
        "stop_pct": stop_pct,
        "target1": round(target1, 2) if target1 is not None else None,
        "target2": round(target2, 2) if target2 is not None else None,
        "trail_stop": round(stop, 2) if stop is not None else None,
        "atr": None,
        "atr_pct": None,
        "pos_pct": round(float(pos_pct), 2),
        "qty_planned": int(qty_planned),
        "qty_filled": 0,
        "entry_price": None,
        "entry_date": None,
        "risk_amount": round((entry_ref - stop) * qty_planned, 2) if stop is not None else None,
        "gain_at_t1": None,
        "gain_at_t2": None,
        "loss_at_stop": None,
        "rr": round(t1_r, 2),
        "exposure": round(timing_scale, 2),
        "state": (state_now or {}).get("state") if state_now else None,
        "capped": False,
        "t1_done": False,
        "entry_mode": mode,
        # 下单时用当前倍率 / 这个冻结值算加减仓幅度
        "regime_label": (state_now or {}).get("label") if state_now else None,
        "timing_scale": timing_scale,
        "last_adj": None,
        "last_adj_reason": None,
        "adj_history": [],
        "source": (str(payload.get("source") or "").strip() or "manual"),
        "note": (str(payload.get("note") or "").strip() or None),
        "last_error": None,
        "created_at": _now_iso(),
        "updated_at": None,
    }
    if stop is not None:
        risk_amount = plan["risk_amount"]
        plan["gain_at_t1"] = round(risk_amount * t1_r, 2)
        plan["gain_at_t2"] = round(risk_amount * t2_r, 2)
        plan["loss_at_stop"] = risk_amount
    with paper.PAPER_LOCK:
        return save_plan(data_dir, plan, account_id)


def ensure_plan_for_fill(
    data_dir: Path,
    account_id: str,
    symbol: str,
    price,
    qty: int,
    *,
    name: str | None = None,
    source: str = "auto",
    note: str | None = None,
) -> dict | None:
    """自动跟单买入成交后, 若该账户没有该标的的活跃计划, 自动补建一份。

    0.3.35: 持仓表新增止损/目标列后, 自动跟单账户买入只走订单路径、
    从不生成交易计划, 列永远显示 '--'。这里用 trade_plan.build_plan 按
    成交价算出止损/目标/仓位参考, 建一份 source="auto" 的计划落盘,
    让持仓行能 join 到价位 (到价减半/清仓跟踪也由计划状态机接管)。

    软失败设计: 任何异常只 log, 绝不影响成交主路径 (调用方在撮合锁内)。
    返回新建计划 dict; 已有活跃计划/无法建计划返回 None。
    """
    try:
        sym = (symbol or "").strip()
        px = float(price) if price is not None else 0.0
        n = int(qty or 0)
        if not sym or px <= 0 or n <= 0:
            return None
        active = list_plans(data_dir, account_id, status=_ACTIVE_STATUSES)
        if any(p.get("symbol") == sym for p in active):
            return None  # 已有活跃计划, 不重复建

        overview = paper.overview(data_dir, account_id=account_id)
        equity = overview.get("total")
        state = trade_plan.latest_state(data_dir)
        built = trade_plan.build_plan(symbol=sym, price=px, equity=equity, name=name, state=state)
        if built is None:
            return None

        payload = {
            "symbol": sym,
            "name": name,
            # 已经按市价成交, 计划只承担价位参考与到价跟踪: 区间取 build_plan
            # 的 ±entry_zone_pct, 执行方式固定 market (不再等区间触发)。
            "entry_low": built["entry_low"],
            "entry_high": built["entry_high"],
            "entry_ref": built["entry_ref"],
            "stop": built["stop"],
            "target1": built["target1"],
            "target2": built["target2"],
            "pos_pct": built["pos_pct"],
            "qty_planned": n,
            "entry_mode": "market",
            "source": source,
            "note": note or f"自动跟单成交后补建 ({built['stop_pct']}% 风险)",
        }
        created = create_plan(data_dir, payload, account_id)
        logger.info("ensure_plan_for_fill: %s %s -> plan %s (stop=%s t1=%s t2=%s)",
                    account_id, sym, created.get("id"), built["stop"], built["target1"], built["target2"])
        return created
    except Exception as e:  # noqa: BLE001 — 补建计划失败绝不影响成交
        logger.warning("ensure_plan_for_fill failed for %s/%s: %s", account_id, symbol, e)
        return None


class CreatedPlans(list):
    """新建计划列表, 附带被跳过的标的与本期风险缩放口径。

    继承 list 是为了兼容既有 `created, state = create_plans_from_rows(...)` 解包
    调用点 (它们的语义完全不变), 需要明细的调用方读这些属性:
      - `skipped`:  没建成计划的标的 [{symbol, reason}] (前端 T11 原样展示);
      - `holdings`: 只数上限缩放口径 (见 trade_plan.effective_holdings_limit);
      - `portfolio`: 组合仓位分配汇总 (缩仓倍数/缩放前后总占比)。
    """

    skipped: list[dict]
    holdings: dict
    portfolio: dict


def create_plans_from_rows(
    data_dir: Path,
    rows: list[dict],
    *,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    cfg: dict | None = None,
    top_n: int | None = None,
    entry_mode: str | None = None,
    rank_key: str = "score",
    rank_desc: bool = True,
) -> tuple[list[dict], dict | None]:
    """策略结果行 → 批量落盘计划; 返回 (新建计划, 大盘情绪状态)。

    同标的有活跃计划时跳过 (避免重复建仓); 大盘情绪缺失时按无择时执行,
    并把 state/exposure 记进计划, 便于前端解释「为什么仓位只有 3 成」。

    entry_mode 缺省取账户 cfg 的 default_entry_mode (默认 zone = 进区间才买);
    显式传入非法值抛 ValueError (由 API 转 400)。

    **优中选优**: 先按 `rank_key` (默认 score) 降序排, 再截断 `top_n`
    (显式传入优先, 未传取 cfg.top_n)。随后按「大盘择时 × 情绪周期」缩放出本期
    最多几只 (`holdings`), 最后做组合仓位分配 (`portfolio`): 总占比超
    cfg.max_total_exposure_pct 时等比缩仓并整手重算股数, 压到不足一手的转 skipped。

    返回的首元素是 CreatedPlans (list 子类), `.skipped` / `.holdings` /
    `.portfolio` 给出明细, 供「挑几只买」的接口回报给前端。
    """
    merged = trade_plan.merge_cfg(cfg if cfg is not None else load_cfg(data_dir, account_id))
    if entry_mode is None or (isinstance(entry_mode, str) and not entry_mode.strip()):
        mode = trade_plan.normalize_entry_mode(None, default=merged["default_entry_mode"])
    else:
        mode = trade_plan.normalize_entry_mode(entry_mode, default=None)
        if mode is None:
            raise ValueError(
                f"entry_mode 只能是 {'/'.join(trade_plan.ENTRY_MODES)}, 收到 {entry_mode!r}"
            )
    state = trade_plan.latest_state(data_dir)
    equity = _f((paper.overview(data_dir, account_id=account_id) or {}).get("total")) or 0.0
    if equity <= 0:
        raise ValueError("账户尚未初始化或权益为 0, 无法生成计划")

    active = {p["symbol"] for p in list_plans(data_dir, account_id, status=_ACTIVE_STATUSES)}
    active_before = set(active)
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    # ① 优中选优: 按评分降序 → 截断 top_n (显式优先, 否则用 cfg)
    if top_n is None:
        limit = int(merged["top_n"])
    else:
        try:
            limit = max(int(top_n), 1)
        except (TypeError, ValueError):
            limit = int(merged["top_n"])
    ranked = trade_plan.rank_rows(rows, rank_key=rank_key, rank_desc=rank_desc)
    picked = ranked[:limit]
    skipped: list[dict] = []
    for row in ranked[limit:]:
        sym = row.get("symbol")
        if isinstance(sym, str) and sym.strip():
            skipped.append({"symbol": sym, "reason": f"未进入优中选优前 {limit} 名, 未生成计划"})

    # ② 只数上限随「大盘择时 × 情绪周期」缩放 (只影响买入只数, 不影响卖出)
    timing = trade_plan.trend_exposure(data_dir)
    holdings = trade_plan.effective_holdings_limit(
        merged, state, timing_exposure=timing.get("exposure")
    )
    hold_limit = holdings.get("limit")
    if hold_limit is not None:
        room = max(int(hold_limit) - len(active_before), 0)
        if room <= 0:
            holdings = dict(holdings, reason=f"{holdings['reason']}; 已有 {len(active_before)} 只活跃计划, 本期无额度")
        capped_rows: list[dict] = []
        for row in picked:
            sym = row.get("symbol")
            if room > 0:
                capped_rows.append(row)
                if isinstance(sym, str) and sym.strip() and sym not in active_before:
                    room -= 1
                    active_before.add(sym)
            else:
                if isinstance(sym, str):
                    skipped.append({
                        "symbol": sym,
                        "reason": f"超本期只数上限 ({hold_limit} 只), 未生成计划",
                    })
        picked = capped_rows
    else:
        holdings = dict(holdings)

    # 已有活跃计划的标的直接跳过 (不重复建单), 进 skipped 明细
    final_picked: list[dict] = []
    for _row in picked:
        _sym = _row.get("symbol")
        if isinstance(_sym, str) and _sym.strip() and _sym in active:
            skipped.append({"symbol": _sym, "reason": "已有活跃计划, 未重复建单"})
        else:
            final_picked.append(_row)
    picked = final_picked

    plans = trade_plan.build_plans_for_rows(
        picked, equity=equity, cfg=merged, state=state, entry_mode=mode,
        rank_key=rank_key, rank_desc=rank_desc,
    )
    # ③ 组合仓位分配: 总占比超上限 → 等比缩仓 + 整手重算, 不足一手转 skipped
    plans, dropped, portfolio = trade_plan.allocate_portfolio(plans, equity=equity, cfg=merged)
    skipped.extend(dropped)
    built_syms = {p["symbol"] for p in plans}
    explained = {s.get("symbol") for s in skipped}
    seen_rows: set[str] = set()
    for row in picked:
        sym = row.get("symbol") if isinstance(row, dict) else None
        if not isinstance(sym, str) or not sym.strip() or sym in seen_rows:
            continue
        seen_rows.add(sym)
        if sym in built_syms or sym in explained:
            continue
        if sym in active:
            skipped.append({"symbol": sym, "reason": "已有活跃计划, 未重复建单"})
        else:
            skipped.append({"symbol": sym, "reason": "股数不足一手或价格无效, 未生成计划"})
    created: list[dict] = []
    with paper.PAPER_LOCK:
        for built in plans:
            if built["symbol"] in active:
                continue
            plan = dict(built)
            plan.update({
                "id": paper._new_id("plan"),
                "account_id": account_id,
                "status": "pending",
                "source": "strategy",
                "note": None,
                "qty_filled": 0,
                "entry_price": None,
                "entry_date": None,
                "trail_stop": built.get("stop"),
                "loss_at_stop": built.get("risk_amount"),
                "t1_done": False,
                "entry_mode": mode,
                "last_error": None,
                # 建计划时只冻结 timing_scale (build_plan 已给), 下单时才写实际系数
                "last_adj": None,
                "last_adj_reason": None,
                "adj_history": [],
                "created_at": _now_iso(),
            })
            created.append(save_plan(data_dir, plan, account_id))
            active.add(built["symbol"])
    out = CreatedPlans(created)
    out.skipped = skipped
    out.holdings = holdings
    out.portfolio = portfolio
    return out, state


# ── 行情驱动: 触发买入 / 止损止盈 ───────────────────────
def _event(plan: dict, kind: str, message: str, price: float | None, account_id: str) -> dict:
    """计划事件 (字段对齐 AlertEvent, 前端通用渲染/语音/留痕可复用)。"""
    return {
        "ts": int(cn_now().timestamp() * 1000),
        "source": "paper",
        "type": kind,
        "severity": "info",
        "rule_id": None,
        "rule_name": "",
        "strategy_id": None,
        "symbol": plan.get("symbol"),
        "name": plan.get("name") or "",
        "message": message,
        "price": round(float(price), 3) if price else None,
        "change_pct": 0,
        "signals": [],
        "conditions": [],
        "logic": "and",
        "account_id": account_id,
        "plan_id": plan.get("id"),
    }


def _plan_orders(orders: list[dict], plan_id: str, side: str | None = None) -> list[dict]:
    """本计划派生的全部订单 (source="plan:{id}"), 可按方向过滤。"""
    src = f"plan:{plan_id}"
    return [
        o for o in orders
        if o.get("source") == src and (side is None or o.get("side") == side)
    ]


def _pending_order_of(orders: list[dict], plan_id: str, side: str) -> dict | None:
    for o in _plan_orders(orders, plan_id, side):
        if o.get("status") == "pending":
            return o
    return None


def _parse_day(value) -> date | None:
    """宽松解析日期 (兼容 '2026-11-02' 与完整 ISO 时间戳前缀)。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _age_days(created_at: str | None, today: str) -> int:
    """计划已挂自然日数 (解析失败按 0, 即不过期)。"""
    start, end = _parse_day(created_at), _parse_day(today)
    if start is None or end is None:
        return 0
    return max((end - start).days, 0)


def _sell_qty(capacity: tuple[int, int] | None, want: int) -> int:
    """卖出数量: 100 整数倍且不超过 T+1 可卖 (扣掉已挂 pending 卖出)。"""
    if capacity is None or want <= 0:
        return 0
    available, pending_sell = capacity
    room = int(available) - int(pending_sell)
    if room <= 0:
        return 0
    return max(min(paper.normalize_qty(int(want)), paper.normalize_qty(room)), 0)


def _reconcile(
    plan: dict,
    positions: dict[str, dict],
    orders: list[dict],
    today: str,
    max_hold_days: int,
) -> bool:
    """把计划状态与真实持仓/订单对齐; 返回是否发生变更。

    判定只看「真实持仓 + 本计划派生的订单状态」, 不看计划自己记的中间字段
    (否则买入单刚挂出、尚未成交时会被误判为已了结):
      - 持仓 > 0                         → holding, 用成交均价回填 entry_price;
      - 持仓为 0 且本计划还有 pending 单 → 继续等 (排队/待成交);
      - 持仓为 0 且买入单确实成交过      → closed (仓位已清);
      - 持仓为 0 且买入单没成交过        → expired + 拒单原因留痕;
      - pending 且超 max_hold_days       → expired (未触发区间)。
    """
    pos = positions.get(plan["symbol"])
    qty = int(pos["qty"]) if pos else 0
    status = plan["status"]
    if status in _LIVE_STATUSES:
        if qty > 0:
            filled = int(float(pos["avg_cost"]) * 10000) / 10000.0
            changed = status != "holding" or plan.get("qty_filled") != qty \
                or plan.get("entry_price") != filled
            plan["status"] = "holding"
            plan["qty_filled"] = qty
            plan["entry_price"] = filled
            if plan.get("entry_date") is None and pos.get("lots"):
                plan["entry_date"] = pos["lots"][0].get("date")
            return changed
        mine = _plan_orders(orders, plan["id"])
        if any(o.get("status") == "pending" for o in mine):
            return False   # 买入排队中 / 卖出待成交, 继续等, 不提前判死
        buys = [o for o in mine if o.get("side") == "buy"]
        if not any(o.get("status") == "filled" for o in buys):
            plan["status"] = "expired"
            plan["last_error"] = (
                (buys[-1].get("reason") if buys else None) or "买入单未成交 (拒单或过期)"
            )
            return True
        plan["status"] = "closed"
        plan["last_error"] = None
        return True
    if status == "pending":
        if qty > 0:   # 已成交但状态未更新 (手工单/上一轮异常中断)
            plan["status"] = "holding"
            plan["qty_filled"] = qty
            plan["entry_price"] = round(float(pos["avg_cost"]), 4)
            return True
        if _pending_order_of(orders, plan["id"], "buy") is not None:
            return False
        buys = _plan_orders(orders, plan["id"], "buy")
        dead = [o for o in buys if o.get("status") in ("expired", "cancelled")]
        if dead:
            plan["status"] = "expired"
            plan["last_error"] = dead[-1].get("reason") or "买入单未被撮合"
            return True
        if _age_days(plan.get("created_at"), today) >= max_hold_days:
            plan["status"] = "expired"
            plan["last_error"] = f"超过 {max_hold_days} 个自然日未触发, 自动过期"
            return True
    return False


def evaluate_plans(
    data_dir: Path,
    snapshot: dict[str, float],
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
) -> list[dict]:
    """盘中钩子: 按最新快照推进所有计划, 返回计划事件列表 (只产出不投递)。

    调用方 (quote_service) 在 evaluate_intraday 之前调用本函数, 使计划触发出来的
    market 单能在同一轮被撮合。逐条计划独立捕获异常: 单个计划出错不影响其它计划,
    更不影响行情监控主链路。

    冻结 (status != "active") 账户直接返回空列表: 计划既不触发买卖也不推进状态
    (不写 last_error / 不做过期), 账户「启动」后从原状态继续。
    """
    acc = paper.get_account(data_dir, account_id)
    if acc is None or acc.get("status") != "active":
        return []
    plans = list_plans(data_dir, account_id)
    if not plans or not snapshot:
        return []
    events: list[dict] = []
    with paper.PAPER_LOCK:
        cfg = load_cfg(data_dir, account_id)
        max_hold_days = int(cfg["max_hold_days"])
        today = cn_today().isoformat()
        positions = paper.load_positions(data_dir, account_id)
        orders = paper.load_orders(data_dir, account_id)
        # 本轮最多取一次雷达系数, 且只有在「有 pending 计划可能买入」时才联网取数;
        # 全是持仓的计划只需中性系数 (卖出保护不看系数, 这是铁律)。
        has_pending = any(p.get("status") == "pending" for p in plans)
        ctx = buy_ctx(
            data_dir,
            account_id,
            cfg=cfg,
            radar=_MISSING if has_pending else _NEUTRAL_RADAR,
        )
        # 硬闸门按「当前仓位」口径用本轮实时快照估值 (只在有 pending 计划时透传)
        ctx["price_map"] = snapshot if has_pending else None
        changed: list[dict] = []
        for plan in plans:
            if plan["status"] in ("closed", "expired", "cancelled"):
                continue
            try:
                before = dict(plan)
                if _reconcile(plan, positions, orders, today, max_hold_days):
                    changed.append(plan)
                px = snapshot.get(plan["symbol"])
                px = float(px) if px is not None and float(px) > 0 else None
                if px is not None:
                    if plan["status"] == "pending":
                        _try_buy(data_dir, plan, px, account_id, orders, events, ctx)
                    elif plan["status"] in _LIVE_STATUSES:
                        _try_sell(data_dir, plan, px, account_id, orders, events)
                if plan != before and plan not in changed:
                    changed.append(plan)
            except Exception as e:  # noqa: BLE001 — 单计划失败不阻断其它计划
                logger.warning("paper plan %s 触发异常: %s", plan.get("id"), e)
                plan["last_error"] = f"触发异常: {e}"
                if plan not in changed:
                    changed.append(plan)
        for plan in changed:
            try:
                save_plan(data_dir, plan, account_id)
            except Exception as e:  # noqa: BLE001 — 落盘失败仅告警, 下一轮重试
                logger.warning("paper plan 保存失败 %s: %s", plan.get("id"), e)
    return events


def _try_buy(
    data_dir: Path,
    plan: dict,
    px: float,
    account_id: str,
    orders: list[dict],
    events: list[dict],
    ctx: dict | None = None,
) -> None:
    """pending 计划: 按计划的 entry_mode 决定买入时机与下单方式。

    三种执行方式 (老计划缺字段一律按 zone):
      - zone      : 现价必须落在 [entry_low, entry_high] 内, 然后挂市价单,
                    当轮 evaluate_intraday 就用现价撮合;
      - market    : 不等区间, 第一轮快照就按现价挂市价单;
      - next_open : 不等区间, 挂次日开盘单 (order_type=next_open),
                    由盘后 settle_day 按次日 raw_open 成交。

    买多少不是一个建计划时就写死的数字: 用「当前大盘倍率 / 建计划时冻结的倍率」
    再乘风险雷达的外围与回撤系数, 得到 adj (见 trade_plan.position_adjust), 然后
    qty = 整手化(qty_planned × adj), 缩到不足一手时买最小一手, 最后受现金与总仓位
    上限约束。adj 与原因写进计划的 last_adj / last_adj_reason / adj_history, 并放进
    事件消息, 用户能直接看到「这单为什么只买了这一点」。
    """
    low = plan.get("entry_low")
    high = plan.get("entry_high")
    if low is None or high is None:
        plan["last_error"] = "买入区间缺失, 计划无法执行"
        return
    mode = trade_plan.normalize_entry_mode(
        plan.get("entry_mode"), default=trade_plan.DEFAULT_ENTRY_MODE
    )
    in_zone = float(low) <= px <= float(high)
    if mode == "zone" and not in_zone:
        return
    if _pending_order_of(orders, plan["id"], "buy") is not None:
        return
    base = int(plan.get("qty_planned") or 0)
    if base < paper.LOT_SIZE:
        plan["last_error"] = "计划股数不足一手, 无法买入"
        return

    ctx = ctx if isinstance(ctx, dict) else buy_ctx(data_dir, account_id)
    cfg = ctx.get("cfg") or load_cfg(data_dir, account_id)
    info = _plan_adj(plan, ctx)
    _note_adj(plan, info)
    qty, why = _sized_qty(
        data_dir, account_id, plan, px, base, float(info["adj"]), cfg
    )
    if qty <= 0:
        plan["last_error"] = why or "动态仓位折算为 0, 本次不买入"
        return

    # 人为闸门 (默认全关: max_symbols=0 表示不限只数)
    max_symbols = int(cfg.get("max_symbols") or 0)
    if max_symbols > 0:
        held = [
            s for s, p in paper.load_positions(data_dir, account_id).items()
            if int((p or {}).get("qty") or 0) > 0
        ]
        if plan["symbol"] not in held and len(held) >= max_symbols:
            plan["last_error"] = f"持仓标的数已达上限 {max_symbols} (cfg.max_symbols), 本次不买入"
            return

    # 0.3.48: 「今日上限 = 硬闸门」。买之前先看「买完后 市值/总资产」会不会
    # 超过 ctx.cap_pct (与决策卡同源: 择时×情绪×外围, 夹 15%~120%)。超了这一笔
    # 直接不买 (不缩量、不改计划状态、留 pending 等盘面转好或仓位降下来再触发)。
    # 卖出路径完全不看这个数 (铁律), 已有持仓一股不动。
    cap_pct = float(ctx.get("cap_pct") or 100.0) if isinstance(ctx, dict) else 100.0
    price_map = ctx.get("price_map") if isinstance(ctx, dict) else None
    if _would_exceed_exposure_cap(data_dir, account_id, px, qty, cap_pct, price_map):
        plan["last_error"] = (
            f"今日上限 {cap_pct:.1f}% (择时×情绪×外围): 买入后仓位将超限, 本次不买入; "
            f"已有持仓不受影响, 降仓由计划触发"
        )
        logger.info("paper plan %s: %s", plan.get("symbol"), plan["last_error"])
        return

    # next_open 交给盘后结算按次日开盘价成交, 其余两种走盘中市价撮合
    order_type = "next_open" if mode == "next_open" else "market"
    try:
        order, err = paper.create_order(
            data_dir,
            plan["symbol"],
            "buy",
            account_id=account_id,
            qty=qty,
            order_type=order_type,
            ref_price=px,
            source=f"plan:{plan['id']}",
            # 只数上限已经在上面按 cfg.max_symbols 判过, 这里不再叠一层固定闸门
            max_position_symbols=max_symbols or None,
        )
    except Exception as e:  # noqa: BLE001 — 建单异常留痕, 留 pending 下轮重试
        plan["last_error"] = f"建单异常: {e}"
        return
    if err or order is None:
        plan["last_error"] = err or "建单失败"
        return
    plan["status"] = "bought"
    plan["entry_ref"] = round(px, 2)
    plan["entry_price"] = round(px, 4)
    plan["entry_date"] = cn_today().isoformat()
    plan["qty_filled"] = 0
    plan["last_error"] = None
    orders.append(order)
    if mode == "zone":
        when = f"区间 {float(low):.2f}~{float(high):.2f}"
    elif mode == "next_open":
        when = "次日开盘价"
    else:
        when = f"立即市价 (区间 {float(low):.2f}~{float(high):.2f})"
    events.append(
        _event(
            plan,
            "plan_buy",
            f"交易计划买入 {plan['symbol']} {qty}股 ({when}, 现价 {float(px):.2f}, "
            f"仓位系数 {float(info['adj']):.2f}: {info['reason']})",
            px,
            account_id,
        )
    )


def _try_sell(
    data_dir: Path,
    plan: dict,
    px: float,
    account_id: str,
    orders: list[dict],
    events: list[dict],
) -> None:
    """holding 计划: 止损/目标1 减半/目标2 清仓。"""
    if _pending_order_of(orders, plan["id"], "sell") is not None:
        return
    capacity = paper.sell_capacity(data_dir, plan["symbol"], account_id)
    # 清仓目标按「持仓股数」算 (可卖数量受 T+1 约束, 不能拿来当清仓目标)
    held = int((paper.load_positions(data_dir, account_id).get(plan["symbol"]) or {}).get("qty") or 0)
    if held <= 0:
        return
    stop = plan.get("stop")
    trail = plan.get("trail_stop")
    t1 = plan.get("target1")
    t2 = plan.get("target2")

    reason: str | None = None
    want = 0
    if stop is not None and px <= float(stop):
        reason, want = f"触及止损 {float(stop):.2f}", held
    elif trail is not None and px <= float(trail):
        reason, want = f"触及移动止损 {float(trail):.2f}", held
    elif t2 is not None and px >= float(t2):
        reason, want = f"触及目标2 {float(t2):.2f}", held
    elif t1 is not None and px >= float(t1) and not plan.get("t1_done"):
        reason, want = f"触及目标1 {float(t1):.2f} 减半", max(int(held // 2), 100)
    if reason is None or want <= 0:
        return

    qty = _sell_qty(capacity, want)
    if qty <= 0:
        # T+1 (当日买入) 或已全部挂出: 留痕但不改状态, 下轮再试
        plan["last_error"] = f"{reason}, 但当前无 T+1 可卖数量 (可卖 0)"
        return

    try:
        order, err = paper.create_order(
            data_dir,
            plan["symbol"],
            "sell",
            account_id=account_id,
            qty=qty,
            order_type="market",
            ref_price=px,
            source=f"plan:{plan['id']}",
        )
    except Exception as e:  # noqa: BLE001
        plan["last_error"] = f"建单异常: {e}"
        return
    if err or order is None:
        plan["last_error"] = err or "建单失败"
        return

    plan["last_error"] = None
    orders.append(order)
    if reason.startswith("触及目标1"):
        plan["t1_done"] = True
        # 保本: 移动止损抬到入场均价 (有成交均价用均价, 否则用参考价)
        base = plan.get("entry_price") or plan.get("entry_ref")
        if base:
            plan["trail_stop"] = round(float(base), 2)
    events.append(
        _event(
            plan,
            "plan_sell",
            f"交易计划卖出 {plan['symbol']} {qty}股 ({reason}, 现价 {px:.2f})",
            px,
            account_id,
        )
    )
