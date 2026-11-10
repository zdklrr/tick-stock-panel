"""风险提示中心: 把「外围风险雷达 + 盘前简报 + 大盘状态/情绪周期」变成主动提示。

设计原则 (逐条都有踩坑依据, 改代码前先读)
--------------------------------------------------------------------------
1. **只在对人有用的时刻说话。** 风险分每分钟都在小幅抖动, 逐次推送会把
   监控中心刷成噪音。本模块只在「发生实质变化」时产出一条事件:
     - 风险等级跨档 (低/正常/偏高/高 之间跳档)
     - 仓位系数变化超过 `EXPOSURE_STEP` (0.10)
     - 盘前简报方向翻转 (偏多/震荡/偏空 之间换档)
     - 大盘状态换档 (强势/偏强/震荡/偏弱/弱势)
     - 情绪周期阶段切换 (由 daily_pipeline 负责, 这里不重复)
2. **绝不因为取数失败而打扰人, 也绝不编数。** 雷达 `ok=False` 或盘前简报
   `ok=False` 时静默跳过, 不清空、不覆盖上一次的已提示状态 —— 否则外围源
   抖一下就会把「已提示偏高」重置成未知, 下一轮又把同样的偏高再报一遍。
3. **频控有下限也有上限。** 同一条事件 `DEDUPE_SEC` 内不重复; 全局最小间隔
   `MIN_TICK_SEC` 内最多产出一条, 避免多个维度同时跨档刷屏。
4. **四路分发与既有告警完全一致** (SSE toast / alert_store 留痕 / 系统通知 /
   Webhook), 复用 quote_service 的既有方法, 不另造一套推送栈。
5. 全部状态落在 `data/user_data/risk_alert_state.json`, 进程重启后不重复播报
   同一档位。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

# ---- 频控 ----------------------------------------------------------------
# 行情轮询最快 10s 一轮, 风险雷达有 45s 缓存; 这里再收一道, 让逻辑本身可以
# 被频繁调用而不产生额外开销。
MIN_TICK_SEC = 120.0
DEDUPE_SEC = 1800.0
# 仓位系数 / 风险分的变化阈值: 太小会被噪声触发, 太大又会漏掉真实的降档。
EXPOSURE_STEP = 0.10
SCORE_STEP = 8.0

# ---- 状态文件 ------------------------------------------------------------
_STATE_FILE = "risk_alert_state.json"

# ---- 提示等级 -> 事件 severity -------------------------------------------
# 与 backend/app/strategy/monitor_rules.py 的 SEVERITIES 对齐。
_LEVEL_RANK = {"low": 0, "normal": 1, "elevated": 2, "high": 3}


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / _STATE_FILE


def load_state(data_dir: Path) -> dict[str, Any]:
    """读已提示状态。文件不存在/损坏一律当成空状态, 绝不抛。"""
    path = _state_path(data_dir)
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:  # noqa: BLE001 — 状态文件损坏不得影响提示链路
        logger.warning("风险提示状态读取失败 (按空状态处理): %s", e)
        return {}


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    """原子写状态文件。写失败只记日志。"""
    path = _state_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001 — 状态落盘失败不应打断行情轮询
        logger.warning("风险提示状态写入失败: %s", e)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%"


def _fmt_mult(value: float | None) -> str:
    if value is None:
        return "—"
    return f"×{value:.2f}"


def _bucket(value: float | None) -> str:
    """把仓位系数归到 0.05 的档位上做去重签名 (避免浮点尾数抖动)。"""
    if value is None:
        return "-"
    return f"{round(float(value) / 0.05) * 0.05:.2f}"


def _severity_for_level(level: str) -> str:
    rank = _LEVEL_RANK.get(level, 1)
    if rank >= 3:
        return "critical"
    if rank >= 2:
        return "warn"
    return "info"


def _event(
    symbol: str,
    name: str,
    message: str,
    *,
    severity: str,
    type_: str,
    price: float | None = None,
    change_pct: float | None = None,
) -> dict[str, Any]:
    """构造与监控中心完全同构的事件字典 (字段缺失会导致前端渲染/留痕异常)。"""
    return {
        "ts": _now_ms(),
        "source": "risk",
        "type": type_,
        "symbol": symbol,
        "name": name,
        "message": message,
        "price": price,
        "change_pct": change_pct if change_pct is not None else 0,
        "severity": severity,
        "rule_id": None,
        "rule_name": "",
        "strategy_id": None,
        "signals": [],
        "conditions": [],
        "logic": "and",
    }


def _dedupe(state: dict[str, Any], key: str, sig: str, now_ms: int) -> bool:
    """同一「变化签名」在 DEDUPE_SEC 内不重复产事件。返回 True 表示应跳过。

    签名里带上变化后的目标值 (等级/系数档), 而不是只用 key —— 否则基线播报
    之后 30 分钟内的真实跨档会被顺手吃掉, 那才是最该提示的时刻。

    另保留一份近期签名表: 等级在 偏高 <-> 正常 之间反复抖动时, 每次都算「变了」,
    但那是噪声, 同一档位在窗口内只提示一次。
    """
    recent = state.get(f"{key}_recent")
    if not isinstance(recent, dict):
        return False
    ts = recent.get(sig)
    if ts is None:
        return False
    return (now_ms - float(ts)) < DEDUPE_SEC * 1000


def _mark(state: dict[str, Any], key: str, sig: str, now_ms: int) -> None:
    state[f"last_{key}_ms"] = now_ms
    recent = state.get(f"{key}_recent")
    if not isinstance(recent, dict):
        recent = {}
    recent[sig] = now_ms
    # 窗口外的条目清掉, 并把表长压到 8 条 (档位组合有限, 防无限增长)
    cutoff = now_ms - DEDUPE_SEC * 1000
    recent = {k: v for k, v in recent.items() if float(v) >= cutoff}
    if len(recent) > 8:
        keep = sorted(recent.items(), key=lambda kv: float(kv[1]))[-8:]
        recent = dict(keep)
    state[f"{key}_recent"] = recent


def build_events(
    data_dir: Path,
    *,
    radar: dict[str, Any] | None = None,
    premarket: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯比较逻辑: 给定三份快照与历史状态, 产出需要提示的事件与更新后的状态。

    参数全为可选, 便于单测注入; 传 None 时由调用方 (evaluate) 去真实取数。
    返回值第二个元素是「应当保存」的新状态 —— 由调用方决定何时落盘。

    **本轮只播一条**是硬约束 (见原则 3), 所以每个维度先把「要播的事件」连同
    「确认它已播出去的提交动作」一起暂存, 等全局频控选完赢家后才提交。落选的
    维度**不推进已提示状态**, 下一轮仍会检出同一个变化并补播 —— 否则最该提示
    的那条会被其它维度的噪音挤掉一次就永久消失 (冒烟实测踩到过)。
    """
    st = dict(state or {})
    now = _now_ms()
    candidates: list[tuple[dict[str, Any], Any]] = []

    # ---------- 1. 外围风险雷达 ----------
    if radar and radar.get("ok"):
        level = str(radar.get("level") or "unknown")
        exposure = radar.get("global_exposure")
        score = radar.get("score")
        prev_level = st.get("radar_level")
        prev_exposure = st.get("radar_exposure")
        prev_rank = _LEVEL_RANK.get(prev_level, -1)
        cur_rank = _LEVEL_RANK.get(level, -1)

        # 首次运行给一次基线播报: 否则若风险一直停在"正常"档, 用户永远收不到
        # 任何提示, 会以为这个功能没生效。状态文件持久化, 所以只在全新环境出现一次。
        baseline = prev_level is None and cur_rank >= 0
        jumped = cur_rank >= 0 and prev_rank >= 0 and cur_rank != prev_rank
        exposure_moved = (
            prev_exposure is not None
            and exposure is not None
            and abs(float(exposure) - float(prev_exposure)) >= EXPOSURE_STEP
        )
        score_moved = (
            st.get("radar_score") is not None
            and score is not None
            and abs(float(score) - float(st["radar_score"])) >= SCORE_STEP
        )

        sig = f"{level}|{_bucket(exposure)}"
        if (baseline or jumped or exposure_moved or score_moved) and not _dedupe(
            st, "radar", sig, now
        ):
            us = (radar.get("regions") or {}).get("us") or {}
            asia = (radar.get("regions") or {}).get("asia") or {}
            breadth = radar.get("breadth") or {}
            equity = radar.get("equity") or {}
            pieces = [
                f"外围风险 {radar.get('level_text') or level}"
                f" (分 {float(score):.0f})" if score is not None else
                f"外围风险 {radar.get('level_text') or level}",
                f"仓位系数 {_fmt_mult(float(exposure) if exposure is not None else None)}",
                f"美股 {_fmt_mult(us.get('exposure'))}",
                f"亚太 {_fmt_mult(asia.get('exposure'))}",
            ]
            if breadth.get("ok"):
                pieces.append(f"跌家占比 {float(breadth.get('dn_ratio') or 0) * 100:.0f}%")
            if equity.get("ok"):
                pieces.append(
                    f"账户回撤 {float(equity.get('drawdown_pct') or 0):.1f}%"
                )
            message = "风险雷达: " + " · ".join(pieces)
            advice = str(radar.get("advice") or "").strip()
            if advice:
                message = f"{message}。{advice}"

            def _commit_radar() -> None:
                _mark(st, "radar", sig, now)
                st["radar_level"] = level
                st["radar_exposure"] = exposure
                st["radar_score"] = score
                st["radar_at"] = radar.get("fetched_at")

            candidates.append((
                _event(
                    symbol="", name="", message=message,
                    severity=_severity_for_level(level), type_="risk_radar",
                ),
                _commit_radar,
            ))
        else:
            # 没播 (未变化或被去重): 观测值照常推进, 避免下一轮误判成"没变过"。
            st["radar_level"] = level
            st["radar_exposure"] = exposure
            st["radar_score"] = score
            st["radar_at"] = radar.get("fetched_at")

    # ---------- 2. 盘前简报 ----------
    if premarket and premarket.get("ok"):
        bias_key = str(premarket.get("bias_key") or "flat")
        prev_bias = st.get("premarket_bias")
        # 只报方向翻转 (首次运行给一次基线); 震荡->震荡 的强度变化不值得打断人。
        if (prev_bias is None or prev_bias != bias_key) and not _dedupe(
            st, "premarket", bias_key, now
        ):
            probs = premarket.get("probs") or {}
            a50 = premarket.get("a50") or {}
            us = premarket.get("us") or {}
            asia = premarket.get("asia") or {}
            gap = premarket.get("gap_pct") or {}
            message = (
                f"盘前简报转为{premarket.get('bias')}: "
                f"高开 {float(probs.get('up') or 0):.0f}% / "
                f"低开 {float(probs.get('down') or 0):.0f}% · "
                f"预计缺口 {gap.get('low')}%~{gap.get('high')}% · "
                f"A50 {_fmt_pct(a50.get('chg_pct'))} · "
                f"美股 {_fmt_pct(us.get('chg_pct'))} · "
                f"亚太 {_fmt_pct(asia.get('chg_pct'))}"
            )
            advice = str(premarket.get("advice") or "").strip()
            if advice:
                message = f"{message}。{advice}"

            def _commit_premarket() -> None:
                _mark(st, "premarket", bias_key, now)
                st["premarket_bias"] = bias_key
                st["premarket_at"] = premarket.get("fetched_at")

            candidates.append((
                _event(
                    symbol="", name="", message=message,
                    severity="warn" if bias_key == "bear" else "info",
                    type_="risk_premarket",
                ),
                _commit_premarket,
            ))
        else:
            st["premarket_bias"] = bias_key
            st["premarket_at"] = premarket.get("fetched_at")

    # ---------- 3. 大盘状态换档 ----------
    if regime and regime.get("state"):
        cur_state = str(regime["state"])
        prev_state = st.get("regime_state")
        if prev_state and prev_state != cur_state and not _dedupe(
            st, "regime", cur_state, now
        ):
            from app.strategy.trade_plan import state_label  # 延迟导入避免循环

            message = (
                f"大盘状态切换: {state_label(prev_state)} → {state_label(cur_state)}"
                f" (分 {regime.get('score')})"
            )
            bad = cur_state in ("weak", "lean_weak")

            def _commit_regime() -> None:
                _mark(st, "regime", cur_state, now)
                st["regime_state"] = cur_state
                st["regime_date"] = regime.get("date")

            candidates.append((
                _event(
                    symbol="", name="", message=message,
                    severity="warn" if bad else "info", type_="risk_regime",
                ),
                _commit_regime,
            ))
        else:
            st["regime_state"] = cur_state
            st["regime_date"] = regime.get("date")

    # ---------- 4. 全局频控: 一轮最多 1 条 ----------
    # 无论本轮有没有播出去, 都把节流时间戳推进: 行情轮询每 10s 一轮, 不推进就会
    # 每轮重算一次; 代价只是真实跨档最多晚 MIN_TICK_SEC (120s) 被检出。
    st["last_tick_ms"] = now
    if not candidates:
        return [], st
    # 保留最严重的 (critical > warn > info); 同级保持「风险雷达 → 盘前 → 状态」
    # 的产出顺序, 风险雷达信息量最大。
    order = {"critical": 2, "warn": 1, "info": 0}
    candidates.sort(
        key=lambda item: order.get(str(item[0].get("severity")), 0), reverse=True)
    event, commit = candidates[0]
    commit()
    return [event], st


def evaluate(data_dir: Path, *, fetch: bool = True) -> list[dict[str, Any]]:
    """取数 → 比较 → 落状态。返回本轮应提示的事件 (可能为空列表)。

    取数失败一律降级: 任一快照拿不到就用 None, 不影响其它维度。
    """
    data_dir = Path(data_dir)
    st = load_state(data_dir)
    # 全局最小间隔: 行情轮询会高频调用, 这里直接早退, 省掉取数与比较开销。
    last_tick = float(st.get("last_tick_ms") or 0)
    if (_now_ms() - last_tick) < MIN_TICK_SEC * 1000:
        return []

    radar: dict[str, Any] | None = None
    premarket: dict[str, Any] | None = None
    regime: dict[str, Any] | None = None

    try:
        from app.services import global_risk

        radar = global_risk.radar_snapshot(data_dir, allow_fetch=fetch)
    except Exception as e:  # noqa: BLE001 — 雷达不可用不得影响提示主流程
        logger.warning("风险雷达取数失败 (跳过该维度): %s", e)

    try:
        from app.services import premarket as premarket_mod

        premarket = premarket_mod.premarket_snapshot(allow_fetch=fetch)
    except Exception as e:  # noqa: BLE001
        logger.warning("盘前简报取数失败 (跳过该维度): %s", e)

    try:
        from app.strategy import trade_plan

        regime = trade_plan.latest_state(data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("大盘状态读取失败 (跳过该维度): %s", e)

    events, new_state = build_events(
        data_dir, radar=radar, premarket=premarket, regime=regime, state=st)
    new_state["updated_at"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
    save_state(data_dir, new_state)
    return events


def dispatch(data_dir: Path, events: list[dict[str, Any]], quote_service: Any = None) -> None:
    """四路分发, 与监控告警 / 模拟盘成交走完全相同的通道。

    quote_service 传了就复用它 (它持有 SSE 订阅者与系统通知/Webhook 的线程池);
    没传时只做 alert_store 留痕, 保证事件不会凭空消失。
    """
    if not events:
        return
    data_dir = Path(data_dir)
    try:
        from app.services import alert_store

        alert_store.append_many(data_dir, events)
    except Exception as e:  # noqa: BLE001 — 留痕失败不影响实时提示
        logger.warning("风险提示留痕失败: %s", e)

    if quote_service is not None:
        try:
            quote_service._broadcast_alerts(events)
            quote_service._maybe_send_system_notifications(events)
            quote_service._maybe_send_paper_webhook(events)
        except Exception as e:  # noqa: BLE001
            logger.warning("风险提示分发失败 (不影响行情轮询): %s", e)


def run_tick(data_dir: Path, quote_service: Any = None, *, fetch: bool = True) -> list[dict[str, Any]]:
    """一次完整 tick: 评估 + 分发 + 返回事件。异常全部吞掉, 绝不冒泡到轮询线程。"""
    try:
        events = evaluate(data_dir, fetch=fetch)
        if events:
            dispatch(data_dir, events, quote_service)
            logger.info("风险提示已推送 %d 条: %s", len(events), events[0].get("message"))
        return events
    except Exception as e:  # noqa: BLE001 — 轮询线程的最后一道防线
        logger.warning("风险提示 tick 异常 (已忽略): %s", e)
        return []


# ---- 非阻塞调度 ----------------------------------------------------------
# 风险雷达一次 tick 要联网 (新浪批量 + 东财逐指数取 MA20), 实测 1~4 秒。
# 行情轮询线程每 10 秒一轮且负责撮合模拟盘, 绝不能被它阻塞 —— 所以放到专用
# 单线程池里跑, 并用 busy 标志保证同一时刻最多一个 tick 在飞。
_executor: Any = None
_busy = False
_busy_lock = threading.Lock()


def _executor_get():
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="risk-alert")
    return _executor


def shutdown() -> None:
    """停机时释放线程池 (幂等)。"""
    global _executor
    ex = _executor
    _executor = None
    if ex is not None:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # noqa: BLE001 — 停机阶段不得抛
            logger.debug("风险提示线程池关闭异常: %s", e)


def schedule(data_dir: Path, quote_service: Any = None, *, fetch: bool = True) -> bool:
    """把一次 tick 丢到后台线程。返回是否真的提交了 (上一轮还没跑完时返回 False)。"""
    global _busy
    with _busy_lock:
        if _busy:
            return False
        _busy = True

    def _job() -> None:
        global _busy
        try:
            run_tick(data_dir, quote_service, fetch=fetch)
        finally:
            with _busy_lock:
                _busy = False

    try:
        _executor_get().submit(_job)
        return True
    except Exception as e:  # noqa: BLE001 — 线程池已关闭/拒绝任务
        with _busy_lock:
            _busy = False
        logger.debug("风险提示任务提交失败: %s", e)
        return False


__all__ = [
    "DEDUPE_SEC",
    "EXPOSURE_STEP",
    "MIN_TICK_SEC",
    "SCORE_STEP",
    "build_events",
    "dispatch",
    "evaluate",
    "load_state",
    "run_tick",
    "save_state",
    "schedule",
    "shutdown",
]
