"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。仓位: 固定金额 或 账户权益百分比。
冷却: 同规则同 symbol 最近一次自动下单后 N 个交易日内不再触发 (按订单
created_at 日期判断, 无独立状态文件 — 可由订单列表重放推导)。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: quote_service._evaluate_monitors 产出的 rule_events →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper
from app.strategy import trade_plan as _tp

logger = logging.getLogger(__name__)


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "auto_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"arule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def validate_rule(rule: dict) -> None:
    """校验规则字段, 非法抛 ValueError (中文信息)。"""
    if (rule.get("name") or "").strip() == "":
        raise ValueError("规则名称不能为空")
    if rule.get("match_kind") not in ("strategy", "rule"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("side") == "auto" and rule.get("match_kind") != "strategy":
        raise ValueError("side=auto (跟信号方向) 仅支持匹配策略 — 监控规则事件无方向语义")
    if rule.get("size_mode") not in ("fixed_amount", "pct_equity"):
        raise ValueError(f"size_mode 非法: {rule.get('size_mode')!r}")
    value = rule.get("size_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("size_value 必须是正数")
    if rule.get("size_mode") == "pct_equity" and value > 100:
        raise ValueError("pct_equity 的 size_value 不能超过 100 (%)")
    if rule.get("position_mode") not in (None, "fixed", "equal_total"):
        raise ValueError(f"position_mode 非法: {rule.get('position_mode')!r} (应为 fixed / equal_total)")
    if rule.get("order_type") not in ("market", "next_open", "close"):
        raise ValueError(f"order_type 非法: {rule.get('order_type')!r}")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")
    max_positions = rule.get("max_positions", 0)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 0:
        raise ValueError("max_positions 必须是非负整数 (0 = 不限制)")


def normalize_rule(rule: dict) -> dict:
    d = dict(rule)
    d["name"] = (d.get("name") or "").strip()
    d["match_id"] = (d.get("match_id") or "").strip()
    # side=auto: 跟随策略信号方向 (入场/进池 → 买入, 出场/移出 → 卖出)。
    # 存量规则缺 side 时按 buy 兼容 (原语义), 新建默认 auto。
    if d.get("match_kind") == "strategy":
        d.setdefault("side", "auto")
    else:
        d.setdefault("side", "buy")
    d.setdefault("size_mode", "fixed_amount")
    d.setdefault("order_type", "next_open")
    d.setdefault("cooldown_days", 5)
    d.setdefault("max_positions", 0)   # 0 = 不限制 (对齐回测 max_positions 语义)
    d.setdefault("position_mode", "fixed")  # fixed=每票固定% / equal_total=总仓均分(建议总仓÷实际只数)
    d.setdefault("enabled", True)
    d.setdefault("created_at", _now_iso())
    return d


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    validate_rule(rule)
    atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json", json.dumps(rule, ensure_ascii=False, indent=2))
    return rule


def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    rule = normalize_rule(json.loads(p.read_text(encoding="utf-8")))
    rule["enabled"] = enabled
    return save_auto_rule(data_dir, rule, account_id)


def _matches(rule: dict, ev: dict) -> bool:
    if rule["match_kind"] == "strategy":
        return ev.get("source") == "strategy" and ev.get("strategy_id") == rule["match_id"]
    return ev.get("rule_id") == rule["match_id"]


def _in_cooldown(data_dir: Path, rule: dict, symbol: str, cooldown_days: int, account_id: str) -> bool:
    """同规则同 symbol 最近一次自动下单是否仍在冷却期 (按日历日, 含当日)。"""
    if cooldown_days <= 0:
        return False
    prefix = f"auto:{rule['id']}"
    today = cn_now().date()
    for order in paper.load_orders(data_dir, account_id):
        if order.get("source") != prefix or order.get("symbol") != symbol:
            continue
        try:
            created = _date.fromisoformat(order["created_at"][:10])
        except (KeyError, ValueError):
            continue
        if (today - created).days < cooldown_days:
            return True
    return False


def _sizing_qty(data_dir: Path, rule: dict, ref_price: float, account_id: str) -> int:
    if ref_price <= 0:
        return 0
    position_mode = rule.get("position_mode", "fixed") or "fixed"
    if rule["size_mode"] == "fixed_amount" or position_mode == "fixed":
        # fixed_amount 模式与 fixed 仓位模式: 直接按规则给定的金额/比例
        if rule["size_mode"] == "fixed_amount":
            amount = float(rule["size_value"])
        else:  # pct_equity + fixed: 每票固定占权益 size_value%
            acc = paper.get_account(data_dir, account_id)
            if acc is None:
                return 0
            nav_rows = paper.load_nav(data_dir, account_id)
            equity = nav_rows[-1]["nav"] if nav_rows else float(acc["cash"])
            amount = equity * float(rule["size_value"]) / 100.0
        return paper.qty_from_amount(amount, ref_price)
    # equal_total: 每票 = 建议总仓 ÷ 计划持仓只数 (受大盘择时×情绪×外围缩放)
    acc = paper.get_account(data_dir, account_id)
    if acc is None:
        return 0
    nav_rows = paper.load_nav(data_dir, account_id)
    equity = nav_rows[-1]["nav"] if nav_rows else float(acc["cash"])
    # 建议总仓% = 100 × 择时 × 情绪 × 外围风险×回撤 (与计划驱动买入同源;
    # 绝不用 synthesize() 的合成 exposure —— 已乘过大盘情绪, 会重复计入)
    timing = 1.0
    phase = 1.0
    max_pos_pct = 20.0
    cfg: dict = {}
    state = None
    try:
        from app.strategy import paper_plans as _pp

        ctx = _pp.buy_ctx(data_dir, account_id)
        cfg = ctx["cfg"]
        state = ctx["state"]
        radar = ctx["radar"]
        timing = _tp.trend_exposure(data_dir).get("exposure") or 1.0
        phase = _tp.exposure_multiplier(state, cfg)
        gexp = float(radar.get("global_exposure") or 1.0)
        efac = float(radar.get("equity_factor") or 1.0)
        radar_adj = min(max(gexp * efac, 0.5), 1.5)
        total_pct = max(0.0, min(100.0, 100.0 * float(timing) * float(phase) * radar_adj))
        max_pos_pct = float(cfg.get("max_pos_pct") or 20.0)
    except Exception as e:  # noqa: BLE001 — 任何缺失一律回退到固定比例
        logger.warning("paper auto equal_total 估算总仓失败 (回退 fixed): %s", e)
        total_pct = float(rule.get("size_value", 0)) or 0.0
    # 分母 = **计划持仓只数**(缩放后的只数上限), 不是「当前已持有 + 1」。
    # 用已持有数会两头出错: 第一票就吃掉 100% 权益(择时中性时), 且后续每票
    # 越买越小、总投入反而超过建议总仓。按计划只数均分才是「总仓均分」。
    raw_max = int(rule.get("max_positions", 0) or 0)
    cfg_max = int(cfg.get("max_symbols") or 0)
    if raw_max > 0:
        # ① 规则自身设了只数上限 → 按缩放后的上限均分 (择时弱 → 只数变少 → 每票变大)。
        #    例: 建议总仓 35%、弱市只买 2 只 → 每票 17.5%; 只买 1 只 → 该票 35%。
        count = _scaled_max_positions(raw_max, {"timing": timing, "phase": phase})
        per_ticket_pct = total_pct / count if count > 0 else total_pct
    elif cfg_max > 0:
        # ② 规则没设, 但交易计划配置里设了 max_symbols → 同源口径 (含 min_holdings)
        limit = _tp.effective_holdings_limit(
            cfg, state, timing_exposure=timing
        ).get("limit")
        count = int(limit or cfg_max)
        per_ticket_pct = total_pct / count if count > 0 else total_pct
    else:
        # ③ 两边都没设 = 不限制只数 → 不知道最终会买几只, 只能按「已占槽 + 本次开仓」
        #    估分母, 并用单票仓位上限 (max_pos_pct, 默认 20%) 兜底: 否则第一票就吃掉
        #    全部权益, 资金预检还会因手续费直接拒单。
        count = max(1, len(_position_slots_used(data_dir, account_id)) + 1)
        per_ticket_pct = min(total_pct / count, max_pos_pct)
    amount = equity * per_ticket_pct / 100.0
    return paper.qty_from_amount(_affordable(amount, acc), ref_price)


def _affordable(amount: float, acc: dict) -> float:
    """把买入金额压到「可用现金能付得起」的范围内 (留 0.5% 给手续费+滑点)。

    建议总仓 100% 时, 最后几票会因手续费超出可用资金被 create_order 预检拒绝
    (「可用资金不足」), 表现为明明有额度却少买一只。这里按现金兜底, 宁可买得
    略少一点, 也不要整票被拒。
    """
    try:
        cash = float(acc.get("cash") or 0.0)
    except (TypeError, ValueError):
        return amount
    return min(amount, cash * 0.995)


def _auto_sizing_coeffs(data_dir: Path, account_id: str) -> dict:
    """自动跟单的仓位系数 {timing, phase, radar_adj, ok}。

    - timing: 大盘择时系数 (trade_plan.trend_exposure, 缺失按 1.0)
    - phase:  情绪周期系数 (trade_plan.exposure_multiplier, 缺失按 1.0)
    - radar_adj: 外围风险 × 账户回撤 (paper_plans._radar_coeffs, 夹 [0.5,1.5])

    与计划驱动买入 (_plan_adj / position_adjust) 同源: **刻意不用风险雷达的合成
    exposure** —— 它已乘过大盘情绪, 会重复计入; 且被夹在 [0.35,1.10], 在最需要
    缩仓时恰好恒等于 1, 把缩仓信号抹平。

    任何环节异常一律按 1.0 (无信号, 不加不减), 绝不抛异常卡住自动跟单。
    """
    out = {"timing": 1.0, "phase": 1.0, "radar_adj": 1.0, "ok": False}
    try:
        from app.strategy import paper_plans as _pp

        cfg = _pp.load_cfg(data_dir, account_id)
        state = _tp.latest_state(data_dir)
        timing = _tp.trend_exposure(data_dir).get("exposure") or 1.0
        phase = _tp.exposure_multiplier(state, cfg)
        radar = _pp._radar_coeffs(data_dir, account_id)
        gexp = float(radar.get("global_exposure") or 1.0)
        efac = float(radar.get("equity_factor") or 1.0)
        radar_adj = min(max(gexp * efac, 0.5), 1.5)
        out = {
            "timing": float(timing),
            "phase": float(phase),
            "radar_adj": radar_adj,
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001 — 择时/雷达缺失时按无信号处理
        logger.warning("paper auto 读取仓位系数失败 (按 1.0 计): %s", e)
    return out


def _scaled_max_positions(max_positions: int, coeffs: dict) -> int:
    """只数上限缩放: max_positions(乐观上限) × 择时 × 情绪, 取整并**夹在 [1, max]**。

    - 上界不能超过用户设的 max_positions: 用户说「最多 5 只」就是最多 5 只,
      择时系数 >1 时不该反而多买 (round(5×1.2)=6 会越界)。
    - 下界保 1: 弱市也不能缩到 0, 否则完全错过行情 (与 plan_config.min_holdings 同理)。
    """
    if max_positions <= 0:
        return 0
    timing = float(coeffs.get("timing") or 1.0)
    phase = float(coeffs.get("phase") or 1.0)
    scaled = int(round(max_positions * timing * phase))
    return min(max(scaled, 1), max_positions)


def _position_slots_used(data_dir: Path, account_id: str) -> set[str]:
    """已占用的持仓槽: 当前持仓标的 + 待成交买单标的 (去重)。
    模拟盘 next_open 单未成交也占槽, 防同日多信号重复开仓超限。"""
    used: set[str] = set()
    for symbol, p in paper.load_positions(data_dir, account_id).items():
        if p["qty"] > 0:
            used.add(symbol)
    for o in paper.load_orders(data_dir, account_id):
        if o.get("status") == "pending" and o["side"] == "buy":
            used.add(o["symbol"])
    return used


_AUTO_BUY_TYPES = {"buy_signal", "pool_entry"}


def _resolve_side(rule: dict, ev: dict) -> str | None:
    """规则方向 → 本次事件实际方向。side=auto (跟策略信号): 入场/进池买,
    出场/移出卖; 其余按规则固定方向。返回 None 表示该事件不适用 (如 auto
    收到无方向语义的事件类型)。"""
    side = rule.get("side", "buy")
    if side != "auto":
        return side
    etype = str(ev.get("type") or "")
    if etype in _AUTO_BUY_TYPES:
        return "buy"
    if etype in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _held_qty(data_dir: Path, symbol: str, account_id: str) -> int:
    p = paper.load_positions(data_dir, account_id).get(symbol)
    return int(p["qty"]) if p else 0


def on_rule_events(data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """监控事件 → 自动下单。返回本次创建的订单列表 (被拒订单只记日志)。

    触发条件: 事件带 symbol 与价格、有规则匹配、未冷却; 下单复用 paper.create_order
    (冻结/资金/T+1/涨跌停等校验自动生效), ref_price 用事件价格 (当前快照价)。
    买入信号受 max_positions 约束 (对齐回测口径): 已持仓 + 待成交买单的标的数
    达到上限后, 新标的开仓被跳过; 已持仓标的加仓与卖出信号不受限。
    """
    created: list[dict] = []
    if not events:
        return created
    rules = load_auto_rules(data_dir, account_id, enabled_only=True)
    if not rules:
        return created
    # 0.3.48: 「今日上限 = 买入硬闸门」。每批事件只算一次 (内部 60s 缓存,
    # 零额外网络), 且只有真正要买入时惰性触发; 卖出路径完全不碰这个数 (铁律)。
    cap_pct: float | None = None
    # 本批事件价当实时快照传给闸门估值 (只用于已有持仓的现价, 纯本地读 parquet)。
    # 逐条 try: 脏事件 (price 是坏字符串) 绝不能把整批自动跟单带崩。
    event_price_map: dict[str, float] = {}
    for e in events:
        sym = (e.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            p = float(e.get("price"))
            if p > 0:
                event_price_map[sym] = p
        except (TypeError, ValueError):
            continue
    with paper.PAPER_LOCK:
        # 同一批事件内复用快照, 由 create_order 后的持仓变化反映 (循环内重取)
        for ev in events:
            symbol = (ev.get("symbol") or "").strip()
            try:
                price = float(ev.get("price"))
            except (TypeError, ValueError):
                continue
            if not symbol or price <= 0:
                continue
            for rule in rules:
                if not _matches(rule, ev):
                    continue
                side = _resolve_side(rule, ev)
                if side is None:
                    continue
                if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id):
                    continue
                if side == "buy":
                    max_positions = int(rule.get("max_positions", 0))
                    if max_positions > 0:
                        # 缩放硬上限: 把 max_positions 当乐观上限, 乘「大盘择时 × 情绪系数」缩放,
                        # 保下限 1 只 (严禁缩到 0 而错过行情), 且不超过用户设的上限。
                        # 卖出/止损完全不参与缩放 (卖出不打折)。
                        coeffs = _auto_sizing_coeffs(data_dir, account_id)
                        scaled_max = _scaled_max_positions(max_positions, coeffs)
                        used = _position_slots_used(data_dir, account_id)
                        if len(used) >= scaled_max and symbol not in used:
                            logger.info(
                                "paper auto %s: %s 跳过开仓 — 已达最大持仓(择时缩放后) %d (=上限%d×择时%.2f×情绪%.2f) (%s)",
                                rule["name"], symbol, scaled_max, max_positions,
                                float(coeffs["timing"]), float(coeffs["phase"]),
                                ", ".join(sorted(used)) or "空",
                            )
                            continue
                    qty = _sizing_qty(data_dir, rule, float(price), account_id)
                else:
                    # auto 的出场卖出 = 全平该标的 (策略出场语义); 固定 sell 规则仍按 sizing。
                    if rule.get("side") == "auto":
                        qty = _held_qty(data_dir, symbol, account_id)
                        if qty <= 0:
                            continue  # 未持仓, 出场信号无意义
                    else:
                        qty = _sizing_qty(data_dir, rule, float(price), account_id)
                        # 金额换算的股数与实际持仓对不上: 以可卖余量封顶, 金额设足够大即清仓
                        held = paper.sell_capacity(data_dir, symbol, account_id)
                        if held is None:
                            continue
                        available, pending_sell = held
                        qty = min(qty, paper.normalize_qty(available - pending_sell))
                if qty <= 0:
                    logger.info("paper auto %s: %s 金额不足以一手 (价 %s)", rule["name"], symbol, price)
                    continue
                if side == "buy":
                    # 买前看「买完后 市值/总资产」是否会超今日上限; 超了这一笔
                    # 直接跳过 (不缩量、不改规则、留日志), 已有持仓一股不卖。
                    try:
                        from app.strategy import paper_plans as _pp

                        if not _pp.SUGGESTED_CAP_ENABLED:
                            pass
                        else:
                            if cap_pct is None:
                                cap_pct = _pp.suggested_exposure_pct(data_dir, account_id)
                            if _pp._would_exceed_exposure_cap(
                                data_dir, account_id, float(price), qty, cap_pct,
                                event_price_map,
                            ):
                                logger.info(
                                    "paper auto %s: %s 跳过买入 — 今日上限 %.1f%%: "
                                    "买入后仓位将超限 (择时×情绪×外围); 已有持仓不受影响",
                                    rule["name"], symbol, cap_pct,
                                )
                                continue
                    except Exception as e:  # noqa: BLE001 — 闸门取数失败按不限制放行
                        logger.warning("paper auto 今日上限闸门取数失败 (放行): %s", e)
                order, err = paper.create_order(
                    data_dir, symbol, side,
                    account_id=account_id,
                    qty=qty,
                    order_type=rule["order_type"],
                    ref_price=float(price),
                    source=f"auto:{rule['id']}",
                    # 事件自带名称就随订单落盘 (维表残缺时持仓仍能显示中文名)
                    name=str(ev.get("name") or "").strip() or None,
                )
                if err:
                    logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, err)
                    continue
                # 触发上下文只留在内存订单上 (不落盘, 不改订单文件 schema):
                # auto_order_events 用它把「是什么触发的」拼进事件, 否则只有 arule id。
                order["auto_meta"] = {
                    "rule_name": str(rule.get("name") or ""),
                    "match_kind": str(rule.get("match_kind") or ""),
                    "match_id": str(rule.get("match_id") or ""),
                    "event_type": str(ev.get("type") or ""),
                    "event_price": float(price),
                }
                created.append(order)
                logger.info("paper auto %s: %s 触发 %s %d 股 (%s)", rule["name"], symbol, side, qty, order["id"])
    return created


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}
# 成交方式短语 (message 用): 与 _ORDER_TYPE_LABEL 同源, 但补足「价成交」语义。
_ORDER_TYPE_PHRASE = {
    "market": "即时价成交",
    "next_open": "次日开盘价成交",
    "close": "当日收盘价成交",
}
# 触发事件类型 → 中文触发原因 (事件里只有 type, 用户看不懂 buy_signal/pool_entry)
_TRIGGER_LABEL = {
    "buy_signal": "策略买入信号",
    "sell_signal": "策略卖出信号",
    "pool_entry": "策略进入股票池",
    "pool_exit": "策略移出股票池",
}
_MATCH_KIND_LABEL = {"strategy": "跟策略", "rule": "跟监控规则"}


def _trigger_label(event_type: str) -> str:
    """监控事件类型 → 人类可读的触发原因。未知类型按「监控规则命中」兜底。"""
    return _TRIGGER_LABEL.get(event_type or "", "监控规则命中")


def format_auto_order_message(ev: dict) -> str:
    """自动跟单事件 → 一句话讲清「谁触发、买什么、多少、怎么成交」。

    name 由调用方回填后再调本函数重建文案 (本域无维表访问权, 生成事件时
    还不知道股票名称), 因此该函数是纯函数, 可被外部按最新字段重算。
    """
    name = str(ev.get("name") or "").strip()
    symbol = str(ev.get("symbol") or "").strip()
    target = f"{name}({symbol})" if name and symbol else (name or symbol)
    side_label = "买入" if ev.get("side") == "buy" else "卖出"
    account_name = str(ev.get("account_name") or "").strip()
    head = (
        f"账户[{account_name}] 自动跟单{side_label} {target} {ev.get('qty')}股"
        if account_name
        else f"自动跟单{side_label} {target} {ev.get('qty')}股"
    )
    parts = [head]

    rule_name = str(ev.get("rule_name") or "").strip()
    match_id = str(ev.get("match_id") or "").strip()
    match_label = _MATCH_KIND_LABEL.get(str(ev.get("match_kind") or ""), "")
    if rule_name:
        rule_text = f"规则「{rule_name}」"
        if match_id and match_label:
            rule_text += f"({match_label} {match_id})"
        parts.append(rule_text)
    reason = str(ev.get("reason") or "").strip()
    if reason:
        parts.append(f"触发: {reason}")
    order_type = str(ev.get("order_type") or "")
    parts.append(_ORDER_TYPE_PHRASE.get(order_type, _ORDER_TYPE_LABEL.get(order_type, order_type)))
    return " · ".join(p for p in parts if p)


def auto_order_events(
    created: list[dict],
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    account_name: str = "",
) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。

    自解释字段: rule_name(规则名) / match_kind+match_id(跟的是策略还是监控规则) /
    reason(触发原因) / account_name(账户名)。`name`(股票名称) 生成时未知 —
    由调用方 (quote_service 模拟盘钩子) 统一回填, 回填后调
    format_auto_order_message() 重建 message, 使文案带上股票名称。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else ""
        meta = o.get("auto_meta") or {}
        match_kind = str(meta.get("match_kind") or "")
        match_id = str(meta.get("match_id") or "")
        ev = {
            "source": "paper",
            "type": "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": o["symbol"],
            "name": "",  # 由调用方按 symbol→name 维表回填
            "rule_id": rule_id,
            "rule_name": str(meta.get("rule_name") or ""),
            "strategy_id": match_id if match_kind == "strategy" else None,
            "match_kind": match_kind,
            "match_id": match_id,
            "side": o["side"],
            "qty": o["qty"],
            "order_type": o["order_type"],
            "account_id": account_id,
            "account_name": account_name or account_id,
            "reason": _trigger_label(str(meta.get("event_type") or "")),
        }
        ev["message"] = format_auto_order_message(ev)
        events.append(ev)
    return events
