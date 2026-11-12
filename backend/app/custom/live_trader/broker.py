"""同花顺客户端驱动器: 把 easytrader 的所有调用钉在一条专用线程上。

为什么必须单线程:
  pywinauto / comtypes 走 Windows COM, COM 对象有公寓(apartment)亲和性 —— 在 A 线程
  创建的对象不能在 B 线程用。easytrader 的 ClientTrader 缓存了 `_main` / `_toolbar`
  等窗口句柄, 跨线程调用会随机失败。同时 GUI 自动化本身也不能并发 (会互相抢焦点)。

因此本模块把「连接 / 读资金 / 读持仓 / 下单 / 撤单」全部序列化到 `live-trader-broker`
这一条守护线程上执行, 调用方通过 Future 拿结果。

安全约束:
  - 只有 `place_order` 类方法会真正发单, 且是否真发由上层 LiveService 的开关决定;
  - 任何异常都被包装成 BrokerError 且带可读中文, 绝不静默吞掉;
  - 断开连接后 `_user` 置 None, 不会残留半死对象。
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout

logger = logging.getLogger(__name__)

# 默认同花顺下单客户端路径 (用户机器实测路径, 可在配置里覆盖)
DEFAULT_EXE_PATH = r"F:\软件安装\同花顺\同花顺\xiadan.exe"

# 单次 GUI 操作的超时 (秒)。easytrader 单笔下单 3~5 秒, 读持仓 1~2 秒。
DEFAULT_TIMEOUT = 30.0
TRADE_TIMEOUT = 60.0


class BrokerError(RuntimeError):
    """实盘驱动层错误, message 面向用户可读。"""


def to_ths_symbol(symbol: str) -> str:
    """`600000.SH` / `600000` / `sh600000` → `sh600000`。

    同花顺客户端需要带市场前缀 (easytrader `_set_trade_params` 只在 security 以
    sh/sz 开头时才切换「上海Ａ股 / 深圳Ａ股」下拉框, 纯 6 位代码不会切换)。
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith("SH") or raw.startswith("SZ"):
        return raw.lower()
    code = raw.split(".")[0]
    if "." in raw:
        suffix = raw.split(".")[-1]
        if suffix in ("SH", "SZ"):
            return f"{suffix.lower()}{code}"
    # 无后缀时按代码段推断交易所
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    return f"sz{code}"


def from_ths_symbol(symbol: str, market: str | None = None) -> str:
    """`sh600000` → `600000.SH`; 已带后缀的原样返回。

    `market` 是同花顺持仓/委托表的「交易市场」列 (如 `上海Ａ股` / `深圳Ａ股`),
    比按代码段推断可靠 —— 可转债、科创板、北交所的代码段规则各有例外
    (实测 `118050` 航宇转债是上海, 按 `1` 开头会误判成深圳)。
    """
    raw = str(symbol or "").strip()
    low = raw.lower()
    code = low[2:] if low.startswith(("sh", "sz")) else raw.split(".")[0]
    if "." in raw and raw.split(".")[-1].upper() in ("SH", "SZ"):
        return raw.upper()
    if low.startswith("sh"):
        return f"{code}.SH"
    if low.startswith("sz"):
        return f"{code}.SZ"
    mkt = str(market or "")
    if "上海" in mkt:
        return f"{code}.SH"
    if "深圳" in mkt:
        return f"{code}.SZ"
    if "北京" in mkt:
        return f"{code}.BJ"
    # 成交表没有「交易市场」列, 只能按代码段推断。国债逆回购两个市场都用
    # 别的段: 上交所 204xxx (GC001 等), 深交所 1318xx。不单独处理的话
    # 204001 会被首位 2 判成深市, 和券商实际归属不符。
    if code.startswith("204"):
        return f"{code}.SH"
    if code.startswith("1318"):
        return f"{code}.SZ"
    return f"{code}.SH" if code.startswith(("6", "5", "9")) else f"{code}.SZ"


def _num(value, default: float = 0.0) -> float:
    """同花顺表格里的数字可能是 str / None / '--', 统一转 float。"""
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        if not text or text in ("--", "-", "None"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


_SHIM_INSTALLED = False


def _side_label(value) -> str:
    """把同花顺「操作」列翻成 buy/sell/other。

    实测取值: 买入 / 卖出 / 回购融券 / 担保品划入 ... 只有前两个能映射到
    我们模拟盘的 side, 其余归 other (展示用, 不参与下单判断)。
    """
    text = str(value or "").strip()
    if "买" in text:
        return "buy"
    if "卖" in text:
        return "sell"
    return "other"


def force_foreground(hwnd: int) -> bool:
    """把窗口真正提到前台 (AttachThreadInput 手法), 成功返回 True。

    为什么要这么绕:
      Windows 只允许「当前前台进程」调用 `SetForegroundWindow`。本程序是
      pywebview/WebView2 桌面壳, 跑起来就占着前台, 于是 broker 线程(非前台)
      调 `SetForegroundWindow` 必然抛
      `(0, 'SetForegroundWindow', 'No error message is available')` ——
      easytrader 读资金/持仓前的 `type_keys` 会走到这里, 导致实盘页一直 409。
      先 `AttachThreadInput` 把自己挂到当前前台线程的输入队列上, 这个限制就
      失效了, 之后再 `BringWindowToTop` + `SetForegroundWindow` + `SetFocus`。
    """
    if not hwnd:
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if user32.GetForegroundWindow() == hwnd:
            return True
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)
        return bool(user32.GetForegroundWindow() == hwnd)
    except Exception as exc:  # noqa: BLE001 - 抢不到焦点不该让业务崩
        logger.warning("force_foreground(hwnd=%s) failed: %s", hwnd, exc)
        return False


def _handle_of(window) -> int:
    """从 pywinauto 的 WindowSpecification / HwndWrapper / 裸 hwnd 里取句柄。"""
    if not window:
        return 0
    if isinstance(window, int):
        return window
    try:
        handle = getattr(window, "handle", None)
    except Exception:  # noqa: BLE001 - WindowSpecification 的属性会真的去查找
        handle = None
    if handle:
        return int(handle)
    try:
        return int(window.wrapper_object().handle)
    except Exception:  # noqa: BLE001
        return 0


_CAPTCHA_SHIM_INSTALLED = False

# 验证码放大倍数: 原图 72x32, Windows OCR 直接认不出, 放大 4~8 倍后稳定正确。
_CAPTCHA_SCALES = (4, 8)
# OCR 出的一定是 4 位数字/字母, 多认出来的噪声字符按这个长度截断。
_CAPTCHA_LEN = 4


def _win_ocr_file(path: str) -> str:
    """用 Windows 自带 OCR (WinRT / winsdk) 识别一张图, 返回原始文本。

    不依赖 tesseract: 那玩意要单独装 exe, 打包进 exe 也不现实。
    """
    import asyncio

    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def run() -> str:
        f = await StorageFile.get_file_from_path_async(path)
        stream = await f.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return ""
        result = await engine.recognize_async(bitmap)
        return result.text or ""

    return asyncio.run(run())


def _captcha_ocr(image_path) -> str:
    """识别同花顺验证码 (4 位数字), 替代 easytrader 里依赖 tesseract 的实现。

    为什么需要: 同花顺把「表格被程序拷贝」视为风险, 弹 `#32770` 验证码框并把
    剪贴板塞成诱饵串 `atls_LISH_drive_current`。easytrader 本来就会自动填验证码
    (`grid_strategies.Copy._get_clipboard_data` 里读 `control_id=0x965` 的 Static
    图片 → OCR → 填 `0x964` Edit → 回车), 但它的 OCR 只走 pytesseract, 而
    tesseract 需要外部 exe。这里换成 Windows 自带 OCR, 同一个填框流程就能走通。
    """
    import tempfile

    from PIL import Image

    src = Image.open(image_path).convert("RGB")
    # 直接认原图会返回空串; 放大 + 灰度后稳定出「1 2 5 1」这种带空格的串。
    best = ""
    tmp_path = None
    try:
        for scale in _CAPTCHA_SCALES:
            big = src.resize(
                (src.width * scale, src.height * scale), Image.LANCZOS
            ).convert("L")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_path = fh.name
            big.save(tmp_path)
            text = _win_ocr_file(tmp_path)
            digits = "".join(ch for ch in text if ch.isalnum())
            if len(digits) == _CAPTCHA_LEN:
                return digits
            if len(digits) > len(best):
                best = digits
        return best[: _CAPTCHA_LEN + 1]
    finally:
        if tmp_path:
            try:
                import os

                os.unlink(tmp_path)
            except OSError:
                pass


def install_captcha_shim() -> bool:
    """把 easytrader 的 `captcha_recognize` 换成 Windows OCR 版本。

    必须同时改两处: `easytrader.utils.captcha` 的定义, 以及
    `easytrader.grid_strategies` 里 import 时绑定好的那个名字。
    """
    global _CAPTCHA_SHIM_INSTALLED
    if _CAPTCHA_SHIM_INSTALLED:
        return True
    try:
        import winsdk.windows.media.ocr  # noqa: F401 - 探测 OCR 是否可用
    except Exception as exc:  # pragma: no cover - 非 Windows / 未装 winsdk
        logger.info("captcha shim skipped (winsdk unavailable): %s", exc)
        return False
    try:
        import easytrader.grid_strategies as gs
        import easytrader.utils.captcha as cap
    except Exception as exc:  # pragma: no cover - easytrader 未装
        logger.info("captcha shim skipped (easytrader unavailable): %s", exc)
        return False

    cap.captcha_recognize = _captcha_ocr
    gs.captcha_recognize = _captcha_ocr
    _CAPTCHA_SHIM_INSTALLED = True
    logger.info("captcha shim installed (Windows OCR)")
    return True


# ── 验证码对话框直捣 ────────────────────────────────────
# 为什么不用 easytrader 自带的过验证码流程:
#   它用 `self._trader.app.top_window()` 定位对话框, 实测拿到的并不是那个
#   `#32770` 验证码框 (top_window 会返回别的窗口), 于是既截不到验证码图、
#   也填不进 Edit、点不中「确定」。改为自己 EnumWindows 找到真正的对话框,
#   对它的子控件截图/填框/点按钮, 一次就过 (实测 8917 等全部正确通过)。
_CAPTCHA_TEXT_HINT = "验证码"
_CAPTCHA_INPUT_HINT = "先输入验证码"
# 对话框里的固定控件 ID (同花顺 xiadan.exe 实测值)
_CID_CAPTCHA_IMAGE = 0x965  # Static, 4 位验证码图
_CID_CAPTCHA_EDIT = 0x964  # Edit, 输入框
_CID_CAPTCHA_ERROR = 0x966  # Static, 「验证码错误!!」
_CID_BTN_OK = 0x1  # Button, 确定
_CID_BTN_CANCEL = 0x2  # Button, 取消
_BM_CLICK = 0x00F5


def _win32():
    import ctypes

    return ctypes.windll.user32


def _enum_tops() -> list[int]:
    import ctypes
    import ctypes.wintypes as wt

    user32 = _win32()
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            out.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def _enum_children(hwnd: int) -> list[int]:
    import ctypes
    import ctypes.wintypes as wt

    user32 = _win32()
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _lparam):
        out.append(h)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def _win_class(hwnd: int) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(128)
    _win32().GetClassNameW(hwnd, buf, 128)
    return buf.value


def _win_text(hwnd: int) -> str:
    # 注意: 跨进程的子控件用 GetWindowTextW 读不到内容 (永远返回空串),
    # 所以这个只用于读顶层窗口标题, 不能用来回读 Edit 是否填进去。
    import ctypes

    n = _win32().GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    _win32().GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def find_captcha_dialog() -> int:
    """返回当前可见的同花顺验证码对话框 hwnd, 没有则 0。"""
    for top in _enum_tops():
        if _win_class(top) != "#32770":
            continue
        has_hint = False
        has_edit = False
        for kid in _enum_children(top):
            cls = _win_class(kid)
            if cls == "Edit":
                has_edit = True
            elif cls == "Static" and _CAPTCHA_INPUT_HINT in _win_text(kid):
                has_hint = True
        if has_hint and has_edit:
            return top
    return 0


def _child_by_cid(hwnd: int, cid: int) -> int:
    user32 = _win32()
    for kid in _enum_children(hwnd):
        if user32.GetDlgCtrlID(kid) == cid:
            return kid
    return 0


def _grab_control(hwnd: int):
    """截取某个控件区域的图像 (跨进程可用)。

    优先 `PrintWindow` 语义的 capture_as_image: 它由目标进程自己绘制, 即使
    对话框被别的窗口遮住也能抓到正确内容。`ImageGrab` 抓的是屏幕像素, 一旦
    同花顺被 TSP / 浏览器挡住就抓到糊图, OCR 结果变成乱码 —— 这是连续读取时
    「验证码突然识别不出来」的真正原因。
    """
    try:
        from pywinauto.controls.hwndwrapper import HwndWrapper

        return HwndWrapper(hwnd).capture_as_image()
    except Exception as exc:  # noqa: BLE001 - 回落到屏幕截图
        logger.warning("capture_as_image 失败, 改用屏幕截图: %s", exc)
        import ctypes
        import ctypes.wintypes as wt

        from PIL import ImageGrab

        rect = wt.RECT()
        _win32().GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        return ImageGrab.grab(bbox=bbox, all_screens=True)


def dismiss_captcha(dlg: int) -> bool:
    """点「取消」关掉验证码框。"""
    btn = _child_by_cid(dlg, _CID_BTN_CANCEL)
    if not btn:
        return False
    _win32().SendMessageW(btn, _BM_CLICK, 0, 0)
    return True


def fill_with_attach(hwnd: int, text: str) -> bool:
    """把文字打进别的进程里的输入框。

    为什么不能直接 `SetFocus` + `SendKeys`: `SetFocus` 是线程局部的 —— 一个
    线程只能把焦点设给自己输入队列里的窗口。同花顺对话框属于 xiadan.exe 的
    线程, 直接 SetFocus 会静默失败, 于是 SendKeys 打到了别的地方, Edit 里
    什么也没有, 点「确定」自然过不了。必须先 `AttachThreadInput` 把自己的线程
    挂到目标线程的输入队列上, 这期间的 SetFocus / 键盘输入才作用到它。
    实测: 不 attach 时连续 5 次全失败, attach 后 6/6 一次通过。
    """
    import ctypes

    import pywinauto.keyboard

    user32 = _win32()
    kernel32 = ctypes.windll.kernel32
    cur_tid = kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    if not tgt_tid:
        return False
    attached = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))
    try:
        parent = user32.GetParent(hwnd) or hwnd
        user32.SetForegroundWindow(parent)
        user32.SetFocus(hwnd)
        import time

        time.sleep(0.15)
        pywinauto.keyboard.SendKeys("^a{DEL}", pause=0.05)
        time.sleep(0.1)
        pywinauto.keyboard.SendKeys(text, pause=0.08, with_spaces=False)
        time.sleep(0.25)
        return True
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)


def pass_captcha(max_tries: int = 5) -> bool:
    """如果存在验证码对话框, 用 Windows OCR 自动填好并提交。

    返回 True 表示「确实出现过验证码, 且已过关」; False 表示当前没有验证码框。
    连续失败则点「取消」并抛 BrokerError, 绝不留下挂着的对话框把后续操作全堵死。
    """
    dlg = find_captcha_dialog()
    if not dlg:
        return False

    import pathlib
    import tempfile
    import time

    # 抢到前台: 填框 + 点按钮都要求对话框是活动窗口
    force_foreground(dlg)
    time.sleep(0.3)

    for attempt in range(1, max_tries + 1):
        edit = _child_by_cid(dlg, _CID_CAPTCHA_EDIT)
        pic = _child_by_cid(dlg, _CID_CAPTCHA_IMAGE)
        ok_btn = _child_by_cid(dlg, _CID_BTN_OK)
        if not (edit and pic and ok_btn):
            logger.warning("captcha dialog 子控件不全, 放弃 (attempt=%d)", attempt)
            break

        tmp_path = None
        try:
            img = _grab_control(pic)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_path = fh.name
            img.save(tmp_path)
            code = _captcha_ocr(tmp_path)
        finally:
            if tmp_path:
                try:
                    pathlib.Path(tmp_path).unlink()
                except OSError:
                    pass

        if len(code) != _CAPTCHA_LEN:
            logger.warning("captcha OCR 结果不可用 (%r), 换一张重试", code)
            # 点验证码图换一张
            _win32().SendMessageW(pic, _BM_CLICK, 0, 0)
            time.sleep(0.6)
            continue

        logger.info("captcha try %d: %s", attempt, code)
        # 填框必须「先挂输入队列再敲键」: set_text/WM_SETTEXT 对同花顺这个 Edit
        # 实测无效 (它是自绘输入框), 而直接 SetFocus 是线程局部的、必失败。
        fill_with_attach(edit, code)
        # 提交
        _win32().SendMessageW(ok_btn, _BM_CLICK, 0, 0)
        time.sleep(1.2)

        if not find_captcha_dialog():
            logger.info("captcha passed on attempt %d", attempt)
            return True

        # 还在: 点验证码图换一张再试
        pic = _child_by_cid(dlg, _CID_CAPTCHA_IMAGE)
        if pic:
            _win32().SendMessageW(pic, _BM_CLICK, 0, 0)
        time.sleep(0.6)

    dismiss_captcha(dlg)
    time.sleep(0.4)
    raise BrokerError(
        "同花顺弹出了拷贝验证码, 自动识别连续失败。请把同花顺窗口点出来, "
        "手动关掉验证码弹窗后再试一次"
    )


def _read_clipboard(retries: int = 5) -> str:

    """读剪贴板文本, 带重试 (另一个程序可能临时占着剪贴板)。"""
    import time

    import pywinauto.clipboard

    for _ in range(max(1, retries)):
        try:
            return pywinauto.clipboard.GetData() or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("clipboard read failed: %s", exc)
            time.sleep(0.3)
    return ""


def build_grid_strategy(
    wait_after_copy: float = 0.8,
    expect_columns: tuple[str, ...] = (),
    renavigate=None,
):
    """构造「会自己过验证码」的 grid 读取策略。

    同花顺对「程序拷贝表格」有保护: 一复制就弹 `#32770` 验证码框, 并把剪贴板
    塞成诱饵串 `atls_LISH_drive_current`。验证码过了之后同花顺才会把真正的
    表格数据放进剪贴板 —— 所以顺序必须是「发复制 → 过验证码 → 再读剪贴板」。

    另一个坑 (v0.3.51 实测): 持仓 / 委托 / 成交三张表**共用同一个 grid 控件**,
    表格内容取决于客户端当前停在哪一页。切页慢一点就会读到上一页的数据 ——
    实测「读持仓」返回了 24 行「当日成交」的表格 (没有股票余额列, 全被映射成 0)。
    因此每张表传 `expect_columns` 做页面校验, 列不匹配就重新切页再读。

    实现上 `expect_columns` / `renavigate` 是**可变的**: 返回的实例带
    `expect(columns, renavigate)` 方法, 调用方在每次读表前设置本次期望,
    避免为三张表各建一个实例 (它们共用同一个 grid 控件)。
    """
    import time

    from easytrader import grid_strategies
    from pywinauto import win32defines

    class CaptchaAwareCopy(grid_strategies.WMCopy):
        """先发复制消息, 遇到验证码就自动过, 再读剪贴板。"""

        def __init__(self) -> None:
            super().__init__()
            self._expect: tuple[str, ...] = tuple(expect_columns)
            self._renavigate = renavigate

        def expect(self, columns: tuple[str, ...], renavigate=None) -> None:
            """设置本次读表的期望列与切页回调 (三张表共用同一实例)。"""
            self._expect = tuple(columns)
            self._renavigate = renavigate

        def _matches(self, rows: list[dict]) -> bool:
            """列名必须包含期望列, 否则说明还停在上一个页面。"""
            if not self._expect:
                return True
            if not rows:
                return False
            cols = set(rows[0].keys())
            return all(c in cols for c in self._expect)

        def get(self, control_id: int):  # noqa: D102 - 覆写父类
            grid = self._get_grid(control_id)
            last_error: Exception | None = None
            for attempt in range(1, 5):
                grid.post_message(win32defines.WM_COMMAND, 0xE122, 0)
                time.sleep(wait_after_copy)
                try:
                    if pass_captcha():
                        # 过了验证码, 同花顺会把数据补进剪贴板
                        time.sleep(0.6)
                except BrokerError as exc:
                    last_error = exc
                    break
                content = _read_clipboard()
                rows = self._format_grid_data(content)
                if not rows:
                    logger.warning(
                        "grid copy attempt %d: 剪贴板无有效表格 (len=%s)",
                        attempt,
                        len(content),
                    )
                    time.sleep(0.5)
                    continue
                if self._matches(rows):
                    return rows
                # 读到了别的页面: 该页可能真的没有数据, 也可能切页还没生效。
                # 让上层重新切一次页 (renavigate), 再复制。
                logger.info(
                    "grid copy attempt %d: 列不匹配 (expect=%s got=%s), 重新切页",
                    attempt,
                    list(self._expect),
                    sorted(rows[0].keys())[:6],
                )
                if self._renavigate is not None:
                    try:
                        self._renavigate()
                    except Exception as exc:  # noqa: BLE001 - 切页失败不该盖住原错误
                        logger.warning("renavigate failed: %s", exc)
                time.sleep(0.8)
            if last_error is not None:
                raise last_error
            return []

    return CaptchaAwareCopy()



def install_foreground_shim() -> bool:
    """把 SetForegroundWindow 换成「附加输入队列再抢焦点」的版本。

    需要覆盖三处, 少一处就会在某些路径上漏掉:
      1. `pywinauto.win32functions.SetForegroundWindow` —— easytrader 的
         `utils.win_gui` 是从这里 import 的;
      2. `easytrader.utils.win_gui.SetForegroundWindow` —— 它在 import 时就把
         名字绑定了, 光改 1 不会影响已绑定的引用;
      3. `pywinauto.controls.hwndwrapper.HwndWrapper.set_focus` —— 它自己
         `import win32gui` 直接调, 不走上面两处, `type_keys` 用的就是它。

    只处理「抢焦点」失败, 其它异常照旧抛出。非 Windows 环境静默跳过。
    """
    global _SHIM_INSTALLED
    if _SHIM_INSTALLED:
        return True
    try:
        from pywinauto import win32functions
        from pywinauto.controls.hwndwrapper import HwndWrapper
    except Exception as exc:  # pragma: no cover - 非 Windows / 未装依赖
        logger.info("foreground shim skipped (pywinauto unavailable): %s", exc)
        return False

    original = win32functions.SetForegroundWindow

    def patched(window, *args, **kwargs):
        hwnd = _handle_of(window)
        if hwnd and force_foreground(hwnd):
            return True
        return original(window, *args, **kwargs)

    win32functions.SetForegroundWindow = patched
    try:
        import easytrader.utils.win_gui as win_gui

        win_gui.SetForegroundWindow = patched
    except Exception as exc:  # pragma: no cover - easytrader 未装
        logger.info("foreground shim: easytrader.win_gui not patched (%s)", exc)

    original_set_focus = HwndWrapper.set_focus

    def set_focus(self, *args, **kwargs):
        hwnd = int(getattr(self, "handle", 0) or 0)
        if hwnd and force_foreground(hwnd):
            # 自己抢到了前台, 就不必再走 pywinauto 会抛异常的那条路径。
            # 但仍要等一小会儿再返回: 紧接着的 type_keys 依赖焦点已生效,
            # pywinauto 原本在 set_focus 末尾也有这个等待。
            try:
                import time

                from pywinauto.timings import Timings

                time.sleep(Timings.after_setfocus_wait)
            except Exception:  # pragma: no cover
                pass
            return self
        return original_set_focus(self, *args, **kwargs)

    HwndWrapper.set_focus = set_focus
    _SHIM_INSTALLED = True
    logger.info("foreground shim installed (AttachThreadInput)")
    return True


def _gui_error(action: str, exc: Exception) -> "BrokerError":
    """把 easytrader/pywinauto 的原始异常翻译成用户能看懂的中文。

    实测常见的三种:
      - `(0, 'SetForegroundWindow', ...)` —— 抢不到前台焦点 (窗口被别的程序占着);
      - `float() argument ... not 'NoneType'` —— 客户端没登录 / 不在资金页面,
        Static 控件读到空串, easytrader 直接 float('') 崩掉, 完全看不出原因;
      - `Window ... is not responding` —— 客户端正忙 (切页/弹窗), 重试即可。
    """
    text = str(exc)
    if "SetForegroundWindow" in text:
        return BrokerError(
            f"{action}失败: 无法把同花顺窗口切到前台 (可能被其它程序占着)。"
            "请把同花顺客户端窗口点出来、确认已登录后重试"
        )
    if "float() argument" in text or "could not convert string to float" in text:
        return BrokerError(
            f"{action}失败: 同花顺界面数据读不到 (通常是没有登录, 或停在非「资金股票」页面)。"
            "请在客户端里完成登录并能看到资金/持仓后再点「连接」"
        )
    if "no active desktop" in text.lower():
        return BrokerError(
            f"{action}失败: 当前没有可用的桌面会话 (锁屏 / 远程桌面断开 / 切换过用户)。"
            "GUI 自动化必须有活动桌面, 请解锁屏幕后重试"
        )
    if "not responding" in text or "is not responding" in text:
        return BrokerError(f"{action}失败: 同花顺客户端正忙 (可能弹了对话框), 请稍后重试")
    if "tesseract" in text.lower():
        return BrokerError(
            f"{action}失败: 同花顺弹出了验证码窗口, 但本机缺少 OCR 组件。"
            "请把同花顺窗口点出来、手动关掉验证码弹窗后重试"
        )
    return BrokerError(f"{action}失败: {text}")


class LiveBroker:
    """把 easytrader 调用序列化到专用线程。"""

    def __init__(self, exe_path: str | None = None) -> None:
        self.exe_path = exe_path or DEFAULT_EXE_PATH
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._user = None  # easytrader ClientTrader, 只能在 broker 线程里碰
        self._main_title: str | None = None
        self._connected_at: str | None = None
        self._last_error: str | None = None

    # ── 线程基础设施 ────────────────────────────────────
    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="live-trader-broker", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        try:  # COM 必须在使用它的线程里初始化
            import pythoncom

            pythoncom.CoInitialize()
        except Exception as exc:  # pragma: no cover - 非 Windows 才会走到
            logger.warning("live broker CoInitialize skipped: %s", exc)
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - 原样交给调用方
                future.set_exception(exc)

    def call(self, fn, *args, timeout: float = DEFAULT_TIMEOUT, **kwargs):
        """在 broker 线程上执行 fn 并等待结果。"""
        self._ensure_started()
        future: Future = Future()
        self._queue.put((fn, args, kwargs, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            raise BrokerError(f"客户端响应超时 ({timeout:.0f} 秒), 请确认同花顺未卡住") from exc

    def stop(self) -> None:
        self._queue.put(None)

    # ── 连接 ────────────────────────────────────────────
    def _expect_grid(self, user, columns: tuple[str, ...], path: list[str]) -> None:
        """在读取某张表之前, 告诉 grid 策略「这次要读哪一页」。

        持仓 / 委托 / 成交 三张表共用同一个 grid 控件: 切页没生效就会读到上一页
        的表格 (实测读持仓拿到 24 行当日成交, 全部字段缺失映射成 0)。所以每次
        读表前先登记期望列, 列不匹配时由策略回调 `path` 重新切页。
        """
        gs = getattr(user, "grid_strategy", None)
        if gs is None or not hasattr(gs, "expect"):
            return

        def _renav() -> None:
            import time

            user._switch_left_menus(path)
            time.sleep(0.4)

        gs.expect(columns, _renav)

    def _do_connect(self):
        try:
            import easytrader
        except Exception as exc:  # pragma: no cover - 仅打包缺失时
            raise BrokerError(f"实盘依赖未安装 (easytrader): {exc}") from exc
        user = easytrader.use("ths")
        # 必须在 connect 之前装好: connect 本身就要抢焦点
        install_foreground_shim()
        # 同花顺把「程序拷贝表格」当成风险操作, 每次读持仓/委托都会弹验证码框,
        # 并把剪贴板塞成诱饵串。easytrader 自带「截图 → OCR → 填框 → 回车」的
        # 自动过验证码流程, 但它的 OCR 只走 pytesseract (需要外部 tesseract.exe)。
        # 这里换成 Windows 自带 OCR, 那条流程就能真正跑通。
        install_captcha_shim()
        # 用自建的 CaptchaAwareCopy: 发复制消息 → 自动过验证码 → 再读剪贴板。
        # easytrader 自带的 Copy/WMCopy 只读剪贴板, 遇到验证码就把诱饵串当表格。
        try:
            user.grid_strategy = build_grid_strategy()
        except Exception as exc:  # pragma: no cover - 版本差异时退回默认
            logger.warning("live trader: 自建 grid 策略不可用, 用默认 (%s)", exc)
        try:
            user.connect(self.exe_path)
        except Exception as exc:
            raise BrokerError(
                f"连接同花顺失败: {exc}。请确认 xiadan.exe 已启动并已登录"
            ) from exc
        self._user = user
        try:
            self._main_title = user.main.window_text()
        except Exception:
            self._main_title = None
        from app.market_time import cn_now

        self._connected_at = cn_now().strftime("%Y-%m-%d %H:%M:%S")
        self._last_error = None
        return {"title": self._main_title, "connected_at": self._connected_at}

    def connect(self) -> dict:
        return self.call(self._do_connect, timeout=TRADE_TIMEOUT)

    def _do_disconnect(self):
        self._user = None
        self._main_title = None
        self._connected_at = None
        return True

    def disconnect(self) -> bool:
        return self.call(self._do_disconnect, timeout=10)

    @property
    def connected(self) -> bool:
        return self._user is not None

    def status(self) -> dict:
        return {
            "connected": self._user is not None,
            "exe_path": self.exe_path,
            "window_title": self._main_title,
            "connected_at": self._connected_at,
            "last_error": self._last_error,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
        }

    # ── 查询 ────────────────────────────────────────────
    def _require_user(self):
        if self._user is None:
            raise BrokerError("尚未连接同花顺客户端, 请先点「连接」")
        return self._user

    def _do_balance(self) -> dict:
        user = self._require_user()
        try:
            raw = user.balance
        except Exception as exc:
            raise _gui_error("读取资金", exc) from exc
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        raw = raw or {}
        return {
            "cash_available": _num(raw.get("可用金额")),
            "cash_balance": _num(raw.get("资金余额")),
            "cash_withdrawable": _num(raw.get("可取金额")),
            "market_value": _num(raw.get("股票市值")),
            "total_asset": _num(raw.get("总资产")),
            "raw": raw,
        }

    def balance(self) -> dict:
        return self.call(self._do_balance, timeout=TRADE_TIMEOUT)

    def _do_positions(self) -> list[dict]:
        user = self._require_user()
        self._expect_grid(
            user, ("证券代码", "股票余额", "可用余额"), ["查询[F4]", "资金股票"]
        )
        try:
            raw = user.position
        except Exception as exc:
            raise _gui_error("读取持仓", exc) from exc
        if isinstance(raw, dict):
            raw = [raw]
        rows: list[dict] = []
        for row in raw or []:
            code = str(row.get("证券代码") or "").strip()
            if not code:
                continue
            # 页面校验已在上层做; 这里再兜一层: 没有「股票余额」列说明读错了表
            if "股票余额" not in row:
                logger.warning("positions: 行缺「股票余额」列, 疑似读错页面, 丢弃")
                continue
            qty = int(_num(row.get("股票余额")))
            available = int(_num(row.get("可用余额")))
            # 冻结一律由「股票余额 − 可用余额」推导: 实测客户端 row 里即便有
            # 冻结数量列也是 0 (000547 余额 7400 / 可用 6600, 冻结列仍报 0),
            # 而可用余额才是真正可卖的权威值 (T+1 当日买入不可卖)。
            frozen = max(0, qty - available)
            market = str(row.get("交易市场") or "").strip()
            rows.append(
                {
                    "symbol": from_ths_symbol(code, market),
                    "code": code,
                    "name": str(row.get("证券名称") or "").strip(),
                    "qty": qty,
                    "available": available,
                    "frozen": frozen,
                    "avg_cost": _num(row.get("成本价")),
                    "price": _num(row.get("市价")),
                    "market_value": _num(row.get("市值")),
                    "pnl": _num(row.get("盈亏")),
                    "pnl_pct": _num(row.get("盈亏比例(%)")),
                    "day_pnl": _num(row.get("当日盈亏")),
                    "position_pct": _num(row.get("仓位占比(%)")),
                    "market": market,
                }
            )
        return rows

    def positions(self) -> list[dict]:
        return self.call(self._do_positions, timeout=TRADE_TIMEOUT)

    def _do_entrusts(self) -> list[dict]:
        user = self._require_user()
        self._expect_grid(
            user, ("证券代码", "委托数量", "委托价格"), ["查询[F4]", "当日委托"]
        )
        try:
            raw = user.today_entrusts
        except Exception as exc:
            raise _gui_error("读取委托", exc) from exc
        if isinstance(raw, dict):
            raw = [raw]
        # 客户端网格末尾常带一整行空行 (证券代码为空), 过滤掉
        rows: list[dict] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("证券代码") or "").strip()
            if not code:
                continue
            # 没有「委托数量」列说明读到的是持仓/成交页, 丢弃该行
            if "委托数量" not in row:
                logger.warning("entrusts: 行缺「委托数量」列, 疑似读错页面, 丢弃")
                continue
            market = str(row.get("交易市场") or "").strip()
            rows.append(
                {
                    "time": str(row.get("委托时间") or "").strip(),
                    "symbol": from_ths_symbol(code, market),
                    "code": code,
                    "name": str(row.get("证券名称") or "").strip(),
                    "side": _side_label(row.get("操作")),
                    "status": str(row.get("备注") or "").strip(),
                    "qty": int(_num(row.get("委托数量"))),
                    "filled_qty": int(_num(row.get("成交数量"))),
                    "price": _num(row.get("委托价格")),
                    "avg_fill_price": _num(row.get("成交均价")),
                    "cancelled_qty": int(_num(row.get("撤消数量"))),
                    "entrust_no": str(row.get("合同编号") or "").strip(),
                    "market": market,
                }
            )
        return rows

    def entrusts(self) -> list[dict]:
        return self.call(self._do_entrusts, timeout=TRADE_TIMEOUT)

    def _do_trades(self) -> list[dict]:
        user = self._require_user()
        self._expect_grid(
            user, ("证券代码", "成交数量", "成交金额"), ["查询[F4]", "当日成交"]
        )
        try:
            raw = user.today_trades
        except Exception as exc:
            raise _gui_error("读取成交", exc) from exc
        if isinstance(raw, dict):
            raw = [raw]
        rows: list[dict] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("证券代码") or "").strip()
            if not code:
                continue
            # 没有「成交数量」列说明读到的是持仓/委托页, 丢弃该行
            if "成交数量" not in row:
                logger.warning("trades: 行缺「成交数量」列, 疑似读错页面, 丢弃")
                continue
            # 成交表不带「交易市场」列, 只能按代码段推断
            rows.append(
                {
                    "time": str(row.get("成交时间") or "").strip(),
                    "symbol": from_ths_symbol(code),
                    "code": code,
                    "name": str(row.get("证券名称") or "").strip(),
                    "side": _side_label(row.get("操作")),
                    "qty": int(_num(row.get("成交数量"))),
                    "price": _num(row.get("成交均价")),
                    "amount": _num(row.get("成交金额")),
                    "entrust_no": str(row.get("合同编号") or "").strip(),
                    "trade_no": str(row.get("成交编号") or "").strip(),
                    "entrust_time": str(row.get("委托时间") or "").strip(),
                }
            )
        return rows

    def trades(self) -> list[dict]:
        return self.call(self._do_trades, timeout=TRADE_TIMEOUT)

    def _do_cancel_all(self) -> dict:
        user = self._require_user()
        try:
            user.cancel_all_entrusts()
        except Exception as exc:
            raise _gui_error("撤单", exc) from exc
        return {"ok": True}

    def cancel_all(self) -> dict:
        return self.call(self._do_cancel_all, timeout=TRADE_TIMEOUT)

    # ── 下单 ────────────────────────────────────────────
    def _do_trade(self, side: str, symbol: str, price: float, qty: int) -> dict:
        user = self._require_user()
        ths = to_ths_symbol(symbol)
        if not ths:
            raise BrokerError(f"无法识别的证券代码: {symbol}")
        if qty <= 0:
            raise BrokerError("委托数量必须大于 0")
        if price <= 0:
            raise BrokerError("委托价格必须大于 0")
        try:
            if side == "buy":
                result = user.buy(ths, price=price, amount=qty)
            elif side == "sell":
                result = user.sell(ths, price=price, amount=qty)
            else:
                raise BrokerError(f"不支持的买卖方向: {side}")
        except BrokerError:
            raise
        except Exception as exc:
            # easytrader 的 TradeError 里就是客户端弹窗原文, 直接给用户看
            message = str(exc) or exc.__class__.__name__
            self._last_error = message
            raise BrokerError(f"客户端拒绝委托: {message}") from exc
        entrust_no = None
        if isinstance(result, dict):
            entrust_no = result.get("entrust_no")
        return {"ok": True, "entrust_no": entrust_no, "ths_symbol": ths}

    def trade(self, side: str, symbol: str, price: float, qty: int) -> dict:
        return self.call(
            self._do_trade, side, symbol, float(price), int(qty), timeout=TRADE_TIMEOUT
        )
