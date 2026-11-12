"""实盘扩展测试: 闸门逻辑 / 符号转换 / 配置合并 / 镜像幂等。

不触碰真实同花顺客户端: 全部用 FakeBroker 替换 LiveBroker, GUI 层单独在
`test_live_trader_broker.py` 里用桩函数验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.custom.live_trader import service as live_service
from app.custom.live_trader.broker import from_ths_symbol, to_ths_symbol
from app.strategy import paper

SYM = "600000.SH"
DAY = "2026-11-12"


class FakeBroker:
    """替身: 记录所有发单调用, 连接状态可控。"""

    def __init__(self, connected: bool = True) -> None:
        self.exe_path = "fake.exe"
        self._connected = connected
        self.trades: list[tuple] = []
        self.raise_on_trade: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "exe_path": self.exe_path,
            "window_title": "网上股票交易系统5.0" if self._connected else None,
            "connected_at": "2026-11-12 09:30:00" if self._connected else None,
            "last_error": None,
            "thread_alive": True,
        }

    def trade(self, side: str, symbol: str, price: float, qty: int) -> dict:
        if self.raise_on_trade:
            raise self.raise_on_trade
        self.trades.append((side, symbol, price, qty))
        return {"ok": True, "entrust_no": f"E{len(self.trades):03d}", "ths_symbol": symbol}

    def balance(self) -> dict:
        return {
            "cash_available": 10000.0,
            "cash_balance": 10000.0,
            "cash_withdrawable": 0.0,
            "market_value": 0.0,
            "total_asset": 10000.0,
            "raw": {},
        }

    def positions(self) -> list[dict]:
        return []

    def connect(self) -> dict:
        self._connected = True
        return {"title": "网上股票交易系统5.0", "connected_at": "2026-11-12 09:30:00"}

    def disconnect(self) -> bool:
        self._connected = False
        return True

    def stop(self) -> None:
        return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    """建好一个模拟账户 + 一个待成交计划单。"""
    monkeypatch.setattr(live_service, "cn_today", lambda: __import__("datetime").date(2026, 11, 12))
    paper.create_account(tmp_path, 1_000_000.0, account_id="acc_live", name="实盘测试")
    return tmp_path


@pytest.fixture
def svc(env: Path, monkeypatch) -> live_service.LiveService:
    broker = FakeBroker()
    service = live_service.LiveService(env, broker=broker)
    # 默认开启 + 非演练, 让用例只测闸门本身
    service.update_config({"enabled": True, "dry_run": False, "accounts": ["acc_live"]})
    return service


def _raw_order(
    env: Path,
    *,
    side: str = "buy",
    price: float | None = 10.0,
    qty: int = 1000,
    source: str = "plan:p1",
    status: str = "pending",
) -> dict:
    """直接落一张 pending 订单文件 (绕过 create_order 的持仓/资金预检)。

    闸门用例只关心 order dict 的字段, 不关心模拟撮合能否成交。
    """
    order = {
        "id": paper._new_id("order"),
        "symbol": SYM,
        "name": None,
        "asset_type": "stock",
        "side": side,
        "qty": qty,
        "order_type": "next_open",
        "status": status,
        "ref_price": price,
        "postponed": 0,
        "source": source,
        "created_at": paper._now_iso(),
        "filled_at": None,
        "fill_price": None,
        "fees": None,
        "reason": None,
    }
    paper.save_order(env, order, account_id="acc_live")
    return order


def _make_order(env: Path, *, side: str = "buy", price: float = 10.0, qty: int = 1000, source: str = "plan:p1") -> dict:
    order, err = paper.create_order(
        env,
        SYM,
        side,
        account_id="acc_live",
        qty=qty,
        order_type="next_open",
        ref_price=price,
        source=source,
    )
    assert err is None, err
    assert order is not None
    return order


# ── 符号转换 ────────────────────────────────────────────
def test_to_ths_symbol_variants():
    assert to_ths_symbol("600000.SH") == "sh600000"
    assert to_ths_symbol("000001.SZ") == "sz000001"
    assert to_ths_symbol("600000") == "sh600000"
    assert to_ths_symbol("000001") == "sz000001"
    assert to_ths_symbol("sh600000") == "sh600000"
    assert to_ths_symbol("SZ000001") == "sz000001"
    assert to_ths_symbol("300750.SZ") == "sz300750"
    assert to_ths_symbol("") == ""


def test_from_ths_symbol_variants():
    assert from_ths_symbol("sh600000") == "600000.SH"
    assert from_ths_symbol("sz000001") == "000001.SZ"
    assert from_ths_symbol("600000.SH") == "600000.SH"
    assert from_ths_symbol("000001") == "000001.SZ"


def test_from_ths_symbol_prefers_client_market():
    """客户端「交易市场」列优先于代码段推断 (118050 转债在上海, 非深圳)。"""
    assert from_ths_symbol("118050", "上海Ａ股") == "118050.SH"
    assert from_ths_symbol("000547", "深圳Ａ股") == "000547.SZ"
    assert from_ths_symbol("688981", "上海Ａ股") == "688981.SH"
    assert from_ths_symbol("920001", "北京Ａ股") == "920001.BJ"
    # 无市场信息时退回代码段推断
    assert from_ths_symbol("118050") == "118050.SZ"


# ── 配置合并 ────────────────────────────────────────────
def test_merge_config_defaults_and_whitelist():
    cfg = live_service.merge_config(None)
    assert cfg["enabled"] is False
    assert cfg["dry_run"] is True
    assert cfg["allow_sell"] is True
    assert cfg["max_orders_per_day"] == 10
    assert cfg["max_order_amount"] == 20000.0


def test_merge_config_ignores_unknown_and_bad_types():
    cfg = live_service.merge_config(
        {
            "enabled": "yes",  # 非 bool → 忽略
            "dry_run": False,
            "max_orders_per_day": 999,  # 越界 → 忽略
            "max_order_amount": -5,  # 越界 → 忽略
            "unknown_key": "x",
            "poll_seconds": 10,
        }
    )
    assert cfg["enabled"] is False
    assert cfg["dry_run"] is False
    assert cfg["max_orders_per_day"] == 10
    assert cfg["max_order_amount"] == 20000.0
    assert "unknown_key" not in cfg
    assert cfg["poll_seconds"] == 10


# ── 闸门 ────────────────────────────────────────────────
def test_disabled_skips_everything(env: Path):
    broker = FakeBroker()
    service = live_service.LiveService(env, broker=broker)
    service.update_config({"enabled": False, "accounts": ["acc_live"]})
    _make_order(env)
    assert service.mirror_once() == []
    assert broker.trades == []


def test_dry_run_logs_but_does_not_trade(env: Path):
    broker = FakeBroker()
    service = live_service.LiveService(env, broker=broker)
    service.update_config(
        {"enabled": True, "dry_run": True, "accounts": ["acc_live"]}
    )
    _make_order(env)
    results = service.mirror_once()
    assert len(results) == 1
    assert results[0]["decision"] == "dry_run"
    assert results[0]["ok"] is True
    assert broker.trades == []
    logs = service.recent_orders()
    assert len(logs) == 1
    assert logs[0]["decision"] == "dry_run"


def test_send_passes_gates_and_trades(svc):
    order = _make_order(svc.data_dir)
    results = svc.mirror_once()
    assert len(results) == 1
    assert results[0]["decision"] == "send"
    assert results[0]["order_id"] == order["id"]
    assert svc.broker.trades == [("buy", SYM, 10.0, 1000)]
    assert results[0]["entrust_no"] == "E001"


def test_recent_orders_hides_lifecycle_events(env: Path):
    """连接/断开/撤单和委托记录写在同一个日志里, 列表只能给委托。

    实测踩过: 页面上 4 条「跳过」空行, 全是 connect 事件被当成委托渲染
    (没有 symbol/side/qty, 表格只能显示「—」和「跳过」)。
    """
    broker = FakeBroker()
    service = live_service.LiveService(env, broker=broker)
    service.update_config({"enabled": True, "dry_run": True, "accounts": ["acc_live"]})

    service.connect()
    _make_order(env)
    service.mirror_once()
    service.disconnect()  # 断开事件也进同一个日志, 但不应出现在委托列表里

    logs = service.recent_orders()
    assert len(logs) == 1
    assert logs[0]["event"] == "mirror"
    assert logs[0]["symbol"] == SYM


def test_second_run_is_idempotent(svc):
    _make_order(svc.data_dir)
    svc.mirror_once()
    svc.mirror_once()
    assert len(svc.broker.trades) == 1


def test_source_not_in_whitelist_is_skipped(svc):
    _make_order(svc.data_dir, source="manual")
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_auto_source_is_mirrored(svc):
    _make_order(svc.data_dir, source="auto")
    results = svc.mirror_once()
    assert len(results) == 1
    assert results[0]["decision"] == "send"


def test_max_orders_per_day_blocks_after_limit(svc):
    svc.update_config({"max_orders_per_day": 1})
    _make_order(svc.data_dir, source="plan:p1")
    _make_order(svc.data_dir, source="plan:p2")
    results = svc.mirror_once()
    assert len(results) == 1
    assert len(svc.broker.trades) == 1


def test_max_order_amount_blocks_large_order(svc):
    svc.update_config({"max_order_amount": 5000.0})
    _make_order(svc.data_dir, price=10.0, qty=1000)  # 10_000 > 5_000
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_allow_sell_false_blocks_sell(svc):
    svc.update_config({"allow_sell": False})
    _raw_order(svc.data_dir, side="sell", qty=1000, source="plan:p2")
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_sell_is_not_scaled_by_timing(svc):
    """铁律: 卖出数量原样透传, 不受任何择时/风险系数影响。"""
    svc.update_config({"allow_sell": True, "max_order_amount": 10_000_000.0})
    _raw_order(svc.data_dir, side="sell", qty=700, source="plan:p9")
    results = svc.mirror_once()
    assert len(results) == 1
    assert svc.broker.trades == [("sell", SYM, 10.0, 700)]


def test_sell_is_mirrored_when_allowed(svc):
    svc.update_config({"allow_sell": True})
    _raw_order(svc.data_dir, side="sell", qty=500, source="plan:p3")
    results = svc.mirror_once()
    assert len(results) == 1
    assert results[0]["decision"] == "send"
    assert svc.broker.trades == [("sell", SYM, 10.0, 500)]


def test_not_connected_skips(svc):
    svc.broker._connected = False
    _raw_order(svc.data_dir)
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_missing_ref_price_skips(svc):
    _raw_order(svc.data_dir, price=None)
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_zero_qty_skips(svc):
    _raw_order(svc.data_dir, qty=0)
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_filled_order_is_skipped(svc):
    _raw_order(svc.data_dir, status="filled")
    assert svc.mirror_once() == []
    assert svc.broker.trades == []


def test_broker_error_is_logged_not_raised(svc):
    from app.custom.live_trader.broker import BrokerError

    svc.broker.raise_on_trade = BrokerError("客户端拒绝委托: 资金不足")
    _make_order(svc.data_dir)
    results = svc.mirror_once()
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "资金不足" in (results[0]["error"] or "")
    # 失败也记入 mirrored, 避免 GUI 反复弹窗重试
    assert svc.mirror_once() == []


def test_snapshot_reports_remaining_quota(svc):
    svc.update_config({"max_orders_per_day": 5})
    _make_order(svc.data_dir)
    svc.mirror_once()
    snap = svc.snapshot()
    assert snap["limits"]["max_orders_per_day"] == 5
    assert snap["limits"]["remaining_orders"] == 4
    assert snap["broker"]["connected"] is True
    assert snap["config"]["enabled"] is True


def test_no_accounts_means_no_scan(env: Path):
    broker = FakeBroker()
    service = live_service.LiveService(env, broker=broker)
    service.update_config({"enabled": True, "dry_run": False, "accounts": []})
    _make_order(env)
    assert service.mirror_once() == []
    assert broker.trades == []


def test_config_persists_across_instances(env: Path):
    first = live_service.LiveService(env, broker=FakeBroker())
    first.update_config({"enabled": True, "dry_run": False, "max_orders_per_day": 3})
    second = live_service.LiveService(env, broker=FakeBroker())
    cfg = second.get_config()
    assert cfg["enabled"] is True
    assert cfg["dry_run"] is False
    assert cfg["max_orders_per_day"] == 3
