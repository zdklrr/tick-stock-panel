"""实盘桥接服务: 把模拟盘账户产生的委托镜像到真实券商客户端。

与模拟盘的关系 (重要):
  - **只读**模拟盘的订单簿 (``paper.load_orders``), 绝不写模拟盘台账、持仓、净值;
  - 实盘账户与模拟盘账户是两套独立账务: 实盘的真实资金/持仓全部从客户端现读;
  - 因此模拟盘的盈亏统计不受实盘成交影响, 反之亦然。

镜像规则:
  - 只看 ``status == "pending"`` 且 ``source`` 命中白名单的订单 (默认 plan: / auto);
  - 每个订单 id 只镜像一次 (``mirrored`` 里记 id, 重启后仍去重);
  - 限价单: 用订单的 ``ref_price``; 缺失时用实时报价, 都没有就跳过并记录原因;
  - 数量: 原样透传, **不做任何择时/风险缩放** (缩放已在模拟盘建计划时算过);
  - 卖出不受择时影响 —— 只要配置允许卖出就照发, 这是铁律。

安全闸门 (全部命中即拒绝该笔, 不影响其它订单):
  1. ``enabled`` 开关关闭          → 全部跳过
  2. ``dry_run`` 演练模式          → 只记录不发单
  3. 当日发单数 >= ``max_orders_per_day``
  4. 单笔金额 > ``max_order_amount``
  5. 卖出且 ``allow_sell`` 为 False
  6. 客户端未连接
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.market_time import cn_now, cn_today

from .broker import DEFAULT_EXE_PATH, BrokerError, LiveBroker

logger = logging.getLogger(__name__)


# 默认只镜像这两个来源 (计划驱动 / 自动跟单); 手动下单不镜像, 避免误操作被放大
DEFAULT_SOURCES = ("plan:", "auto")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "exe_path": DEFAULT_EXE_PATH,
    "accounts": [],
    "sources": list(DEFAULT_SOURCES),
    "allow_sell": True,
    "max_orders_per_day": 10,
    "max_order_amount": 20000.0,
    "poll_seconds": 5,
}


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def merge_config(raw: dict | None) -> dict:
    """只用白名单键, 类型不对就回落默认值 (与 trade_plan.merge_cfg 同风格)。"""
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    if isinstance(raw.get("enabled"), bool):
        cfg["enabled"] = raw["enabled"]
    if isinstance(raw.get("dry_run"), bool):
        cfg["dry_run"] = raw["dry_run"]
    exe = raw.get("exe_path")
    if isinstance(exe, str) and exe.strip():
        cfg["exe_path"] = exe.strip()
    accounts = raw.get("accounts")
    if isinstance(accounts, list):
        cfg["accounts"] = [str(a).strip() for a in accounts if str(a).strip()]
    sources = raw.get("sources")
    if isinstance(sources, list):
        cleaned = [str(s).strip() for s in sources if str(s).strip()]
        if cleaned:
            cfg["sources"] = cleaned
    if isinstance(raw.get("allow_sell"), bool):
        cfg["allow_sell"] = raw["allow_sell"]
    cap = _num(raw.get("max_orders_per_day"), -1)
    if 0 <= cap <= 200:
        cfg["max_orders_per_day"] = int(cap)
    amount = _num(raw.get("max_order_amount"), -1)
    if 0 < amount <= 10_000_000:
        cfg["max_order_amount"] = float(amount)
    poll = _num(raw.get("poll_seconds"), -1)
    if 2 <= poll <= 300:
        cfg["poll_seconds"] = int(poll)
    return cfg


class LiveService:
    """实盘镜像服务。线程安全: 所有状态改动都在 ``self._lock`` 内。"""

    def __init__(self, data_dir: Path, *, broker: LiveBroker | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "user_data" / "extensions" / "live_trader"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.stopping = threading.Event()
        self.broker = broker or LiveBroker()
        self._config = self._load_config()
        self.broker.exe_path = self._config["exe_path"]
        self._state = self._load_state()

    # ── 落盘 ────────────────────────────────────────────
    def _config_path(self) -> Path:
        return self.root / "config.json"

    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _log_path(self) -> Path:
        return self.root / "live_orders.jsonl"

    def _load_config(self) -> dict:
        try:
            raw = json.loads(self._config_path().read_text(encoding="utf-8"))
        except Exception:
            raw = None
        return merge_config(raw)

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("mirrored", [])
                data.setdefault("day", cn_today().isoformat())
                data.setdefault("day_count", 0)
                return data
        except Exception:
            pass
        return {"mirrored": [], "day": cn_today().isoformat(), "day_count": 0}

    def _save_config(self) -> None:
        from app.services.fs_utils import atomic_write_text

        atomic_write_text(
            self._config_path(),
            json.dumps(self._config, ensure_ascii=False, indent=2),
        )

    def _save_state(self) -> None:
        from app.services.fs_utils import atomic_write_text

        # mirrored 只保留最近 500 条, 防止无限增长
        if len(self._state["mirrored"]) > 500:
            self._state["mirrored"] = self._state["mirrored"][-500:]
        atomic_write_text(
            self._state_path(),
            json.dumps(self._state, ensure_ascii=False, indent=2),
        )

    def _append_log(self, entry: dict) -> None:
        try:
            with self._log_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover
            logger.warning("live order log write failed: %s", exc)

    # ── 配置 ────────────────────────────────────────────
    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def update_config(self, patch: dict) -> dict:
        with self._lock:
            merged = dict(self._config)
            merged.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG})
            self._config = merge_config(merged)
            self._save_config()
            self.broker.exe_path = self._config["exe_path"]
            return dict(self._config)

    # ── 状态汇总 ────────────────────────────────────────
    def snapshot(self) -> dict:
        """给前端的一次性状态快照 (不触发 GUI 操作)。"""
        with self._lock:
            cfg = dict(self._config)
            state = {
                "day": self._state.get("day"),
                "day_count": int(self._state.get("day_count") or 0),
                "mirrored_count": len(self._state.get("mirrored") or []),
            }
        today = cn_today().isoformat()
        if state["day"] != today:
            state["day_count"] = 0
        return {
            "config": cfg,
            "broker": self.broker.status(),
            "state": state,
            "limits": {
                "max_orders_per_day": cfg["max_orders_per_day"],
                "max_order_amount": cfg["max_order_amount"],
                "remaining_orders": max(
                    0, int(cfg["max_orders_per_day"]) - int(state["day_count"])
                ),
            },
        }

    def recent_orders(self, limit: int = 100) -> list[dict]:
        """镜像委托记录。

        只返回 event == "mirror" 的条目 —— 同一个日志文件里还混着 connect /
        disconnect / cancel_all 这类生命周期事件, 它们没有 symbol/side/qty
        字段, 前端表格会渲染成一堆「跳过」空行。
        """
        path = self._log_path()
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("event") != "mirror":
                        continue
                    out.append(entry)
        except Exception as exc:
            logger.warning("live order log read failed: %s", exc)
            return []
        return out[-max(1, min(int(limit), 500)):][::-1]

    # ── 连接管理 ────────────────────────────────────────
    def connect(self) -> dict:
        info = self.broker.connect()
        self._append_log(
            {"at": cn_now().isoformat(timespec="seconds"), "event": "connect", **info}
        )
        return self.broker.status()

    def disconnect(self) -> dict:
        try:
            self.broker.disconnect()
        except BrokerError:
            pass
        self._append_log({"at": cn_now().isoformat(timespec="seconds"), "event": "disconnect"})
        return self.broker.status()

    def account_info(self) -> dict:
        """真实资金 + 持仓 (只读)。"""
        balance = self.broker.balance()
        positions = self.broker.positions()
        return {"balance": balance, "positions": positions}

    # ── 镜像核心 ────────────────────────────────────────
    def _source_allowed(self, source: str) -> bool:
        src = str(source or "")
        return any(src.startswith(prefix) for prefix in self._config["sources"])

    def _resolve_price(self, order: dict) -> float:
        price = _num(order.get("ref_price"))
        if price > 0:
            return price
        return 0.0

    def _decide(self, order: dict, day_count: int, today: str) -> dict:
        """判断是否该发这一笔。返回 {action, reason}。

        action: send / dry_run / skip
        """
        cfg = self._config
        if not cfg["enabled"]:
            return {"action": "skip", "reason": "实盘开关未开启"}
        if order["id"] in set(self._state.get("mirrored") or []):
            return {"action": "skip", "reason": "该委托已镜像过"}
        source = str(order.get("source") or "manual")
        if not self._source_allowed(source):
            return {"action": "skip", "reason": f"委托来源 {source} 不在镜像白名单"}
        if str(order.get("status")) != "pending":
            return {"action": "skip", "reason": f"委托状态 {order.get('status')} 非待成交"}
        side = str(order.get("side") or "")
        if side == "sell" and not cfg["allow_sell"]:
            return {"action": "skip", "reason": "配置禁止实盘卖出"}
        if day_count >= int(cfg["max_orders_per_day"]):
            return {
                "action": "skip",
                "reason": f"已达当日发单上限 {int(cfg['max_orders_per_day'])} 笔",
            }
        price = self._resolve_price(order)
        if price <= 0:
            return {"action": "skip", "reason": "缺少参考价, 无法限价委托"}
        qty = int(_num(order.get("qty")))
        if qty <= 0:
            return {"action": "skip", "reason": "委托数量为 0"}
        amount = price * qty
        if amount > float(cfg["max_order_amount"]):
            return {
                "action": "skip",
                "reason": f"单笔金额 {amount:.0f} 超过上限 {cfg['max_order_amount']:.0f}",
            }
        if not self.broker.connected:
            return {"action": "skip", "reason": "未连接同花顺客户端"}
        if cfg["dry_run"]:
            return {"action": "dry_run", "reason": "演练模式 (dry_run), 不真正发单"}
        return {"action": "send", "reason": "通过全部闸门"}

    def mirror_once(self, accounts: list[str] | None = None) -> list[dict]:
        """扫一遍指定账户的待成交委托, 逐一决策。返回本次处理的条目。"""
        from app.strategy import paper

        with self._lock:
            cfg = dict(self._config)
            today = cn_today().isoformat()
            if self._state.get("day") != today:
                self._state["day"] = today
                self._state["day_count"] = 0
        if not cfg["enabled"]:
            return []
        targets = accounts if accounts is not None else (cfg["accounts"] or [])
        if not targets:
            return []
        out: list[dict] = []
        for account_id in targets:
            try:
                orders = paper.load_orders(self.data_dir, account_id)
            except Exception as exc:
                logger.warning("live mirror: load orders failed %s: %s", account_id, exc)
                continue
            for order in orders:
                with self._lock:
                    decision = self._decide(
                        order, int(self._state.get("day_count") or 0), today
                    )
                if decision["action"] == "skip":
                    # 只记录「已启用但被闸门挡住」的有意义拒绝, 避免刷屏
                    continue
                entry = self._execute(order, account_id, decision)
                out.append(entry)
        return out

    def _execute(self, order: dict, account_id: str, decision: dict) -> dict:
        side = str(order.get("side"))
        symbol = str(order.get("symbol"))
        price = self._resolve_price(order)
        qty = int(_num(order.get("qty")))
        name = str(order.get("name") or "")
        entry: dict[str, Any] = {
            "at": cn_now().isoformat(timespec="seconds"),
            "event": "mirror",
            "account": account_id,
            "order_id": order.get("id"),
            "symbol": symbol,
            "name": name,
            "side": side,
            "price": price,
            "qty": qty,
            "amount": round(price * qty, 2),
            "source": order.get("source"),
            "decision": decision["action"],
            "reason": decision["reason"],
            "ok": False,
            "entrust_no": None,
            "error": None,
        }
        if decision["action"] == "dry_run":
            entry["ok"] = True
        elif decision["action"] == "send":
            try:
                result = self.broker.trade(side, symbol, price, qty)
                entry["ok"] = True
                entry["entrust_no"] = result.get("entrust_no")
            except BrokerError as exc:
                entry["error"] = str(exc)
                logger.warning("live mirror trade failed %s %s: %s", symbol, side, exc)
            except Exception as exc:  # pragma: no cover
                entry["error"] = f"未预期错误: {exc}"
                logger.exception("live mirror trade unexpected")
        # 无论成功与否都记入 mirrored (失败不重试, 避免 GUI 反复弹窗)
        with self._lock:
            self._state.setdefault("mirrored", []).append(order.get("id"))
            if decision["action"] == "send":
                self._state["day_count"] = int(self._state.get("day_count") or 0) + 1
            self._save_state()
        self._append_log(entry)
        # 镜像结果只写扩展自己的日志; 模拟盘订单保持原样 (两套账务隔离)
        return entry

    # ── 实时委托/成交 (透传客户端) ──────────────────────
    def live_entrusts(self) -> list[dict]:
        return self.broker.entrusts()

    def live_trades(self) -> list[dict]:
        return self.broker.trades()

    def cancel_all(self) -> dict:
        result = self.broker.cancel_all()
        self._append_log(
            {"at": cn_now().isoformat(timespec="seconds"), "event": "cancel_all", **result}
        )
        return result

    # ── 后台循环 ────────────────────────────────────────
    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                if self._config.get("enabled"):
                    self.mirror_once()
            except Exception:
                logger.exception("live trader worker failed")
            self.stopping.wait(int(self._config.get("poll_seconds") or 5))
