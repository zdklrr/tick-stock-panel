"""实盘驱动层测试: 用假 easytrader 模块验证字段映射与错误包装, 不碰真实 GUI。

LiveBroker 把一切调用钉在专用线程上, 这里直接用它的 `_do_*` 私有方法
(同步执行, 不经过队列), 既避开 COM 依赖, 又能验证映射逻辑。
"""

from __future__ import annotations

import sys
import types

import pytest

from app.custom.live_trader.broker import BrokerError, LiveBroker, _num


def test_num_handles_messy_values():
    assert _num(None) == 0.0
    assert _num("") == 0.0
    assert _num("--") == 0.0
    assert _num("-") == 0.0
    assert _num("None") == 0.0
    assert _num("1,234.5") == 1234.5
    assert _num("12.30") == 12.3
    assert _num(7) == 7.0
    assert _num("abc", 9.0) == 9.0


def test_balance_mapping(monkeypatch):
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        balance={
            "资金余额": "285.91",
            "可用金额": "11,619.16",
            "可取金额": 285.91,
            "股票市值": "97532.00",
            "总资产": "109151.85",
        }
    )
    out = broker._do_balance()
    assert out["cash_available"] == 11619.16
    assert out["cash_balance"] == 285.91
    assert out["cash_withdrawable"] == 285.91
    assert out["market_value"] == 97532.0
    assert out["total_asset"] == 109151.85


def test_balance_accepts_list_payload():
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(balance=[{"可用金额": "100"}])
    assert broker._do_balance()["cash_available"] == 100.0


def test_positions_mapping():
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        position=[
            {
                "证券代码": "000547",
                "证券名称": "航天发展",
                "股票余额": 7400,
                "可用余额": 6600,
                "成本价": 22.289,
                "市价": 13.18,
                "市值": 97532.0,
                "盈亏": -67409.27,
                "盈亏比例(%)": -40.869,
                "当日盈亏": 296.0,
                "仓位占比(%)": 89.35,
            }
        ]
    )
    rows = broker._do_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "000547.SZ"
    assert row["code"] == "000547"
    assert row["name"] == "航天发展"
    assert row["qty"] == 7400
    assert row["available"] == 6600
    assert row["frozen"] == 7400 - 6600
    assert row["avg_cost"] == pytest.approx(22.289)
    assert row["pnl_pct"] == pytest.approx(-40.869)


def test_positions_skips_rows_without_code():
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        position=[{"证券名称": "无名"}, {"证券代码": "600000", "股票余额": 100}]
    )
    rows = broker._do_positions()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600000.SH"


def test_positions_ignores_client_frozen_column():
    """实测客户端「冻结数量」列恒为 0, 冻结必须靠余额差推导。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        position=[
            {
                "证券代码": "000547",
                "股票余额": 7400,
                "可用余额": 6600,
                "冻结数量": 0,  # 客户端报 0, 不可信
            }
        ]
    )
    row = broker._do_positions()[0]
    assert row["frozen"] == 800


def test_positions_uses_client_market_column():
    """含「交易市场」列时以客户端为准 (转债/科创板代码段规则有例外)。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        position=[
            {
                "证券代码": "118050",
                "证券名称": "航宇转债",
                "股票余额": 0,
                "可用余额": 0,
                "交易市场": "上海Ａ股",
            },
            {
                "证券代码": "000547",
                "证券名称": "航天发展",
                "股票余额": 100,
                "可用余额": 100,
                "交易市场": "深圳Ａ股",
            },
        ]
    )
    rows = broker._do_positions()
    assert rows[0]["symbol"] == "118050.SH"
    assert rows[1]["symbol"] == "000547.SZ"


def test_require_user_raises_when_not_connected():
    broker = LiveBroker()
    with pytest.raises(BrokerError, match="尚未连接"):
        broker._do_balance()


def test_trade_rejects_bad_inputs():
    broker = LiveBroker()
    broker._user = types.SimpleNamespace()
    with pytest.raises(BrokerError, match="委托数量"):
        broker._do_trade("buy", "600000.SH", 10.0, 0)
    with pytest.raises(BrokerError, match="委托价格"):
        broker._do_trade("buy", "600000.SH", 0.0, 100)
    with pytest.raises(BrokerError, match="证券代码"):
        broker._do_trade("buy", "", 10.0, 100)
    with pytest.raises(BrokerError, match="买卖方向"):
        broker._do_trade("hold", "600000.SH", 10.0, 100)


def test_trade_maps_entrust_no_and_prefix():
    calls = []

    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        buy=lambda sec, price, amount: (
            calls.append(("buy", sec, price, amount)) or {"entrust_no": "E001"}
        ),
        sell=lambda sec, price, amount: (
            calls.append(("sell", sec, price, amount)) or {"entrust_no": "E002"}
        ),
    )
    assert broker._do_trade("buy", "600000.SH", 10.0, 100)["entrust_no"] == "E001"
    assert broker._do_trade("sell", "000001.SZ", 9.5, 200)["entrust_no"] == "E002"
    assert calls == [
        ("buy", "sh600000", 10.0, 100),
        ("sell", "sz000001", 9.5, 200),
    ]


def test_trade_wraps_client_rejection():
    def boom(*_a, **_kw):
        raise RuntimeError("资金不足")

    broker = LiveBroker()
    broker._user = types.SimpleNamespace(buy=boom)
    with pytest.raises(BrokerError, match="客户端拒绝委托: 资金不足"):
        broker._do_trade("buy", "600000.SH", 10.0, 100)
    # 具体原因要留在 status 里, 前端能看见
    assert broker.status()["last_error"] == "资金不足"


def test_connect_reports_missing_dependency(monkeypatch):
    """easytrader 不可用时给出可读错误, 而不是 ModuleNotFoundError。"""
    broker = LiveBroker()
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "easytrader":
            raise ImportError("no module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(BrokerError, match="实盘依赖未安装"):
        broker._do_connect()


def test_connect_reports_gui_failure(monkeypatch):
    def bad_connect(_path):
        raise RuntimeError("Process not found")

    fake = types.SimpleNamespace(use=lambda _b: types.SimpleNamespace(connect=bad_connect))
    monkeypatch.setitem(sys.modules, "easytrader", fake)
    broker = LiveBroker()
    with pytest.raises(BrokerError, match="连接同花顺失败"):
        broker._do_connect()


def test_status_reflects_connection_state():
    broker = LiveBroker()
    st = broker.status()
    assert st["connected"] is False
    assert st["exe_path"].endswith("xiadan.exe")
    broker._user = types.SimpleNamespace()
    broker._main_title = "网上股票交易系统5.0"
    st = broker.status()
    assert st["connected"] is True
    assert st["window_title"] == "网上股票交易系统5.0"


# ── 前台焦点修复 (0.3.50) ───────────────────────────────
# 背景: TSP 自己是 WebView2 桌面壳, 运行时占着前台。Windows 只允许前台进程
# 抢焦点, 于是 easytrader 切页面时的 SetForegroundWindow 必失败, 实盘页读
# 资金 100% 报错。修法是 AttachThreadInput 附加到前台线程再抢。

def test_install_foreground_shim_is_idempotent(monkeypatch):
    """重复安装不应层层包裹, 否则每次调用都多一层。"""
    from app.custom.live_trader import broker as mod

    monkeypatch.setattr(mod, "_SHIM_INSTALLED", False)
    try:
        import pywinauto.win32functions as wf
        from pywinauto.controls.hwndwrapper import HwndWrapper
    except Exception:  # pragma: no cover - 非 Windows
        pytest.skip("pywinauto 不可用")
    original_sf = wf.SetForegroundWindow
    original_focus = HwndWrapper.set_focus
    try:
        assert mod.install_foreground_shim() is True
        patched_sf = wf.SetForegroundWindow
        patched_focus = HwndWrapper.set_focus
        # 第二次调用直接返回, 不重复包装
        assert mod.install_foreground_shim() is True
        assert wf.SetForegroundWindow is patched_sf
        assert HwndWrapper.set_focus is patched_focus
        assert patched_sf is not original_sf
        assert patched_focus is not original_focus
    finally:
        wf.SetForegroundWindow = original_sf
        HwndWrapper.set_focus = original_focus
        mod._SHIM_INSTALLED = False


def test_shimmed_set_foreground_prefers_force_foreground(monkeypatch):
    """有句柄时走 force_foreground; 抢不到才回退原实现。"""
    from app.custom.live_trader import broker as mod

    monkeypatch.setattr(mod, "_SHIM_INSTALLED", False)
    calls = {"force": [], "orig": []}

    def fake_force(hwnd):
        calls["force"].append(hwnd)
        return True

    monkeypatch.setattr(mod, "force_foreground", fake_force)
    try:
        import pywinauto.win32functions as wf
        from pywinauto.controls.hwndwrapper import HwndWrapper
    except Exception:  # pragma: no cover
        pytest.skip("pywinauto 不可用")
    original_sf = wf.SetForegroundWindow
    original_focus = HwndWrapper.set_focus
    try:
        mod.install_foreground_shim()
        patched = wf.SetForegroundWindow

        class Win:
            handle = 4242

        assert patched(Win()) is True
        assert calls["force"] == [4242]
    finally:
        import pywinauto.win32functions as wf2

        wf2.SetForegroundWindow = original_sf
        HwndWrapper.set_focus = original_focus
        mod._SHIM_INSTALLED = False


def test_force_foreground_handles_zero_handle():
    from app.custom.live_trader.broker import force_foreground

    assert force_foreground(0) is False


def test_handle_of_accepts_various_shapes():
    from app.custom.live_trader.broker import _handle_of

    assert _handle_of(None) == 0
    assert _handle_of(1234) == 1234

    class WithHandle:
        handle = 77

    assert _handle_of(WithHandle()) == 77

    class Raising:
        @property
        def handle(self):
            raise RuntimeError("window gone")

    assert _handle_of(Raising()) == 0


def test_gui_error_translates_foreground_failure():
    """`(0, 'SetForegroundWindow', ...)` 必须翻译成能照着做的中文。"""
    from app.custom.live_trader.broker import BrokerError, _gui_error

    exc = _gui_error(
        "读取资金", OSError(0, "SetForegroundWindow", "No error message is available")
    )
    assert isinstance(exc, BrokerError)
    assert "同花顺窗口" in str(exc)
    assert "登录" in str(exc)


def test_gui_error_translates_not_logged_in():
    """客户端半登录时 Static 读到空串, easytrader 抛 float(None) TypeError。"""
    from app.custom.live_trader.broker import _gui_error

    exc = _gui_error(
        "读取资金", TypeError("float() argument must be a string or a real number, not 'NoneType'")
    )
    assert "没有登录" in str(exc)


def test_gui_error_translates_busy_window():
    from app.custom.live_trader.broker import _gui_error

    exc = _gui_error("读取持仓", RuntimeError("Window is not responding!"))
    assert "正忙" in str(exc)


def test_gui_error_keeps_unknown_text():
    from app.custom.live_trader.broker import _gui_error

    exc = _gui_error("读取资金", ValueError("something odd"))
    assert "something odd" in str(exc)


def test_gui_error_translates_captcha_popup():
    """默认 Copy 策略会触发验证码弹窗, easytrader 去调 tesseract 然后报缺依赖。"""
    from app.custom.live_trader.broker import _gui_error

    exc = _gui_error(
        "读取持仓",
        RuntimeError("tesseract is not installed or it's not in your PATH."),
    )
    assert "验证码" in str(exc)
    assert "关掉" in str(exc)


def test_connect_uses_captcha_aware_grid_strategy():
    """必须换成自建的 CaptchaAwareCopy。

    默认 `Copy` 用 type_keys 复制、遇到验证码靠 pytesseract (要外部 exe);
    `WMCopy` 不发按键但仍然会被拷贝保护弹验证码。自建策略是唯一能过的:
    发复制消息 → 用 Windows OCR 过验证码 → 再读剪贴板。
    """
    import sys as _sys
    import types as _types

    from easytrader import grid_strategies as gs

    class FakeUser:
        grid_strategy = gs.Copy

        def connect(self, _path):
            pass

        @property
        def main(self):
            class M:
                @staticmethod
                def window_text():
                    return "网上股票交易系统5.0"

            return M()

    fake = _types.SimpleNamespace(use=lambda _b: FakeUser(), grid_strategies=gs)
    orig = _sys.modules.get("easytrader")
    _sys.modules["easytrader"] = fake
    try:
        broker = LiveBroker()
        broker._do_connect()
        # 若没换策略, grid_strategy 会停在 gs.Copy
        assert isinstance(broker._user.grid_strategy, gs.WMCopy)
        assert broker._user.grid_strategy.__class__.__name__ == "CaptchaAwareCopy"
    finally:
        if orig is not None:
            _sys.modules["easytrader"] = orig
        else:
            _sys.modules.pop("easytrader", None)


def test_gui_error_translates_no_active_desktop():
    """锁屏 / 远程桌面断开时 pywinauto 会报 no active desktop, 要翻成人话。"""
    from app.custom.live_trader.broker import _gui_error

    exc = _gui_error(
        "读取资金",
        RuntimeError("There is no active desktop required for moving mouse cursor!"),
    )
    text = str(exc)
    assert "桌面会话" in text
    assert "解锁" in text


def test_balance_uses_translated_error(monkeypatch):
    """_do_balance 抛出的必须是可读中文, 不是原始 pywinauto 文本。"""
    broker = LiveBroker()

    class Boom:
        @property
        def balance(self):
            raise OSError(0, "SetForegroundWindow", "No error message is available")

    broker._user = Boom()
    with pytest.raises(BrokerError, match="同花顺窗口"):
        broker._do_balance()


# ── 验证码自动过 ────────────────────────────────────────
def test_find_captcha_dialog_returns_zero_when_none():
    """没有验证码框时必须安全返回 0, 不能抛异常。"""
    from app.custom.live_trader import broker as B

    assert B.find_captcha_dialog() == 0


def test_pass_captcha_returns_false_without_dialog():
    """没有验证码时 pass_captcha 返回 False (表示「没遇到过」), 不算失败。"""
    from app.custom.live_trader import broker as B

    assert B.pass_captcha() is False


def test_fill_with_attach_returns_false_on_zero_handle():
    """无效句柄不能崩, 必须返回 False。"""
    from app.custom.live_trader import broker as B

    assert B.fill_with_attach(0, "1234") is False


def test_dismiss_captcha_returns_false_without_cancel_button():
    from app.custom.live_trader import broker as B

    assert B.dismiss_captcha(0) is False


def test_captcha_ocr_rejects_non_image():
    """OCR 传非图片路径时不能抛未捕获异常以外的错 (返回空串即可)。"""
    from app.custom.live_trader import broker as B

    with pytest.raises(Exception):
        B._captcha_ocr(r"G:\__no_such_file__.png")


def test_side_label_maps_ths_operations():
    """同花顺「操作」列的取值要映射到 buy/sell/other。"""
    from app.custom.live_trader.broker import _side_label

    assert _side_label("买入") == "buy"
    assert _side_label("卖出") == "sell"
    assert _side_label("回购融券") == "other"
    assert _side_label("担保品划入") == "other"
    assert _side_label("") == "other"
    assert _side_label(None) == "other"


def test_entrusts_mapping(monkeypatch):
    """委托表字段映射: 中文列 → 英文字段, 并丢掉末尾空行。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        today_entrusts=[
            {
                "委托时间": "14:58:28",
                "证券代码": "000547",
                "证券名称": "航天发展",
                "操作": "买入",
                "备注": "已申报确认",
                "委托数量": 100,
                "成交数量": 0,
                "委托价格": 12.86,
                "成交均价": 0.0,
                "撤消数量": 0,
                "合同编号": "122326",
                "交易市场": "深圳Ａ股",
            },
            {"证券代码": "", "证券名称": ""},  # 客户端网格末尾的空行
        ]
    )
    rows = broker._do_entrusts()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "000547.SZ"
    assert row["name"] == "航天发展"
    assert row["side"] == "buy"
    assert row["status"] == "已申报确认"
    assert row["qty"] == 100
    assert row["price"] == 12.86
    assert row["entrust_no"] == "122326"


def test_trades_mapping(monkeypatch):
    """成交表字段映射: 没有「交易市场」列, 按代码段推断交易所。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        today_trades=[
            {
                "成交时间": "14:59:22",
                "证券代码": "204001",
                "证券名称": "GC001",
                "操作": "回购融券",
                "成交数量": 8,
                "成交均价": 1.44,
                "成交金额": 8000.0,
                "合同编号": "122834",
                "成交编号": 1200801007098448,
                "委托时间": "14:59:22",
            },
            {"证券代码": ""},
        ]
    )
    rows = broker._do_trades()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "204001.SH"  # 204xxx 是上交所国债逆回购
    assert row["side"] == "other"
    assert row["qty"] == 8
    assert row["price"] == 1.44
    assert row["amount"] == 8000.0
    assert row["trade_no"] == "1200801007098448"


def test_positions_drops_rows_from_wrong_page():
    """读持仓时若拿到别的页面的表格 (没有「股票余额」列), 必须丢弃而不是映射成 0。

    实测踩过: 切页没生效时「读持仓」返回了 24 行「当日成交」的表, 全部字段
    缺失被映射成 qty=0/price=0, 界面上就会显示一堆 0 股持仓。
    """
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        position=[
            {"证券代码": "204001", "证券名称": "GC001", "成交数量": 8, "成交金额": 8000.0},
            {
                "证券代码": "000547",
                "证券名称": "航天发展",
                "股票余额": 7500,
                "可用余额": 7500,
            },
        ]
    )
    rows = broker._do_positions()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "000547.SZ"
    assert rows[0]["qty"] == 7500


def test_entrusts_drops_rows_from_wrong_page():
    """读委托时若拿到别的页面的表格 (没有「委托数量」列), 必须丢弃。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        today_entrusts=[
            {"证券代码": "000547", "证券名称": "航天发展", "股票余额": 7500},
            {
                "证券代码": "000547",
                "证券名称": "航天发展",
                "委托数量": 100,
                "委托价格": 12.86,
                "操作": "买入",
            },
        ]
    )
    rows = broker._do_entrusts()
    assert len(rows) == 1
    assert rows[0]["qty"] == 100
    assert rows[0]["side"] == "buy"


def test_trades_drops_rows_from_wrong_page():
    """读成交时若拿到别的页面的表格 (没有「成交数量」列), 必须丢弃。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        today_trades=[
            {"证券代码": "000547", "股票余额": 7500},
            {
                "证券代码": "000547",
                "证券名称": "航天发展",
                "成交数量": 100,
                "成交均价": 12.93,
                "成交金额": 1293.0,
            },
        ]
    )
    rows = broker._do_trades()
    assert len(rows) == 1
    assert rows[0]["qty"] == 100
    assert rows[0]["price"] == pytest.approx(12.93)


def test_grid_strategy_expect_switches_target_columns():
    """三张表共用同一个 grid 控件, expect() 必须能切换本次期望列。"""
    from app.custom.live_trader.broker import build_grid_strategy

    gs = build_grid_strategy()
    assert gs._matches([{"证券代码": "x"}])  # 未设期望时一律匹配

    gs.expect(("证券代码", "股票余额"), None)
    assert gs._matches([{"证券代码": "x", "股票余额": 1}])
    assert not gs._matches([{"证券代码": "x", "成交数量": 1}])
    assert not gs._matches([])

    # 切到委托的期望后, 持仓的列就不该再匹配
    gs.expect(("证券代码", "委托数量"), None)
    assert gs._matches([{"证券代码": "x", "委托数量": 1}])
    assert not gs._matches([{"证券代码": "x", "股票余额": 1}])


def test_expect_grid_is_noop_without_captcha_strategy():
    """用户自带的 grid 策略没有 expect() 时不能炸 (向后兼容)。"""
    broker = LiveBroker()
    broker._user = types.SimpleNamespace(
        grid_strategy=types.SimpleNamespace(),
        position=[{"证券代码": "600000", "股票余额": 100, "可用余额": 100}],
    )
    rows = broker._do_positions()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600000.SH"

