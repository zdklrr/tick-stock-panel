"""新浪行情 HTTP 客户端 (免费, 无需 Key)。

职责: 内部标准代码 → 新浪代码的转换、单标的分钟K 请求、响应数组解包。
不解析业务字段, 不知道 provider / services 层。

接口 (2026-11 实测):
  GET https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
      ?symbol=sh600000&scale=1&ma=no&datalen=1023
  顶层就是 JSON 数组 (不是 {code,data} 信封):
      [{"day":"2026-11-02 14:09:00","open":"10.050","high":"10.080",
        "low":"10.050","close":"10.070","volume":"371900","amount":"3742919.0377"}, ...]
  - day 已是北京墙钟字符串, 无需时区换算
  - volume 单位「股」, amount 单位「元」 (股→手 由 provider 层折算)
  - scale ∈ {1,5,15,30,60}; datalen 实测 1023 正常 (1500 也返回, 1999 起返回字面量
    null), 契约按 1023 封顶: 1023 根 1m ≈ 4.3 个交易日
  - 必须带 User-Agent, 否则可能 403
"""

from __future__ import annotations

import httpx

BASE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

# 实测: 1023 根 1m(≈4.3 交易日)稳定返回; 更大值上游可能直接给 null, 故封顶。
MAX_DATALEN = 1023
SCALES = (1, 5, 15, 30, 60)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_MARKET_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


class SinaError(Exception):
    """新浪行情接口错误(代码无法识别 / 网络失败 / HTTP 非 200 / 响应不是数组)。"""


def to_sina_symbol(symbol: str) -> str:
    """内部标准代码 (600000.SH) → 新浪代码 (sh600000); 无法识别返回空串。

    兼容三种入参: 已带市场前缀 (sh600000)、带交易所后缀 (600000.SH)、裸 6 位码。
    裸码按首位推断市场 (仅兜底, 内部代码一律带后缀)。
    """
    text = str(symbol or "").strip()
    if not text:
        return ""
    low = text.lower()
    if len(low) > 2 and low[:2] in ("sh", "sz", "bj") and low[2:].isdigit():
        return low
    if "." in text:
        code, _, suffix = text.partition(".")
        code = code.strip()
        prefix = _MARKET_PREFIX.get(suffix.strip().upper())
        return f"{prefix}{code}" if prefix and code.isdigit() else ""
    if text.isdigit() and len(text) == 6:
        return f"{_infer_prefix(text)}{text}"
    return ""


def _infer_prefix(code: str) -> str:
    """裸 6 位码 → 市场前缀。9 开头歧义: 920xxx 属北交所, 其余 9 为沪 B 股。"""
    head = code[0]
    if head in ("4", "8") or code.startswith("920"):
        return "bj"
    if head in ("5", "6", "9"):
        return "sh"
    return "sz"


class SinaClient:
    """新浪行情 REST 客户端 (httpx.Client 线程安全, 可并发复用)。"""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        self._base_url = base_url
        self._http = httpx.Client(headers={"User-Agent": _UA}, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def minute_bars(
        self, symbol: str, scale: int = 1, datalen: int = MAX_DATALEN
    ) -> list[dict]:
        """拉单标的分钟K 原始数组。

        返回上游 dict 列表(未做字段映射); 正常无数据返回 []。
        网络/HTTP/结构异常抛 SinaError, 由 provider 决定软硬失败语义。
        """
        sina_symbol = to_sina_symbol(symbol)
        if not sina_symbol:
            raise SinaError(f"无法识别标的代码: {symbol!r}")
        if scale not in SCALES:
            raise SinaError(f"不支持的 scale: {scale!r} (支持 {SCALES})")
        try:
            length = int(datalen)
        except (TypeError, ValueError) as e:
            raise SinaError(f"datalen 非法: {datalen!r}") from e
        params = {
            "symbol": sina_symbol,
            "scale": scale,
            "ma": "no",
            "datalen": max(1, min(length, MAX_DATALEN)),
        }
        try:
            resp = self._http.get(self._base_url, params=params)
        except httpx.HTTPError as e:
            raise SinaError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise SinaError(f"HTTP {resp.status_code}: {sina_symbol}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise SinaError(f"响应不是 JSON: {sina_symbol}") from e
        if payload is None:
            # datalen 过大或标的不存在时上游直接回字面量 null
            return []
        if not isinstance(payload, list):
            raise SinaError(f"响应不是数组: {sina_symbol} ({type(payload).__name__})")
        return [row for row in payload if isinstance(row, dict)]


__all__ = [
    "BASE_URL",
    "MAX_DATALEN",
    "SCALES",
    "SinaClient",
    "SinaError",
    "to_sina_symbol",
]
