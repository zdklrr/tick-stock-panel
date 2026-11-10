"""腾讯行情(qt.gtimg.cn)HTTP 客户端。

端点与实测口径 (2026-11 验证):
  GET https://qt.gtimg.cn/q=sh600000,sz000001,sh000001
  - 单次可放 50~100 只 (实测 100 只耗时 0.04s), 返回行数可能少于请求数
    (停牌/退市/代码不存在时服务端直接不返回该行), 缺失的由调用方跳过。
  - 响应为 GBK 文本, 每行形如 `v_sh600000="1~浦发银行~600000~10.06~...";`。
  - 必须带 User-Agent, 否则可能被拒绝。
  - 个股(sh)/指数(sz 399xxx, sh 000xxx)/ETF(sh510300)/北交所(bj430047)
    可在同一请求里混排, 字段布局一致。

本模块只负责「取原始字段数组」: 不做单位换算, 也不解释字段语义
(字段索引口径见 provider.py 模块 docstring)。
"""

from __future__ import annotations

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://qt.gtimg.cn/q="

# 腾讯要求常规浏览器 UA; 缺 UA 时可能直接 403。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

_EXCHANGE_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
_PREFIX_EXCHANGE = {prefix: exch for exch, prefix in _EXCHANGE_PREFIX.items()}

# 每行形如 v_sh600000="...";  (只认 2 位字母前缀 + 6 位数字)
_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')

# 实测 100 只/请求正常返回; 取 80 留余量并降低被限流概率。
BATCH_SIZE = 80
BATCH_INTERVAL_S = 0.05


class TencentError(Exception):
    """腾讯行情接口异常(网络/HTTP/响应结构), 由 provider 转成软失败。"""


def to_tencent_code(symbol: str) -> str | None:
    """内部 symbol(600000.SH) → 腾讯代码(sh600000)。无法映射时返回 None。"""
    text = (symbol or "").strip()
    if not text:
        return None
    if "." in text:
        code, _, exchange = text.rpartition(".")
        prefix = _EXCHANGE_PREFIX.get(exchange.upper())
        if prefix is None or not code.isdigit():
            return None
        return f"{prefix}{code}"
    low = text.lower()
    if len(low) == 8 and low[:2] in _PREFIX_EXCHANGE and low[2:].isdigit():
        return low
    return None


def to_standard_symbol(code: str) -> str | None:
    """腾讯代码(sh600000) → 内部 symbol(600000.SH)。无法映射时返回 None。"""
    low = (code or "").strip().lower()
    if len(low) != 8 or not low[2:].isdigit():
        return None
    exchange = _PREFIX_EXCHANGE.get(low[:2])
    if exchange is None:
        return None
    return f"{low[2:]}.{exchange}"


def parse_quotes(text: str) -> dict[str, list[str]]:
    """GBK 文本 → {内部 symbol: 按 "~" 分割的字段数组}。

    结构异常的行(前缀/代码不合法)直接跳过, 不抛异常; 是否整体异常由调用方判断。
    """
    out: dict[str, list[str]] = {}
    for match in _LINE_RE.finditer(text or ""):
        symbol = to_standard_symbol(match.group(1))
        if symbol is None:
            continue
        out[symbol] = match.group(2).split("~")
    return out


class TencentClient:
    """qt.gtimg.cn 批量快照客户端(同步 httpx)。"""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        batch_size: int = BATCH_SIZE,
        interval_s: float = BATCH_INTERVAL_S,
    ) -> None:
        self._base_url = base_url
        self._batch_size = max(1, int(batch_size))
        self._interval_s = max(0.0, float(interval_s))
        self._http = httpx.Client(headers=_HEADERS, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def fetch_batch(self, symbols: list[str]) -> dict[str, list[str]]:
        """单次请求取一批快照。网络/HTTP/结构异常抛 TencentError。"""
        codes: list[str] = []
        for symbol in symbols:
            code = to_tencent_code(symbol)
            if code is not None:
                codes.append(code)
        if not codes:
            return {}

        url = f"{self._base_url}{','.join(codes)}"
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise TencentError(f"请求腾讯行情失败: {e}") from e
        if resp.status_code != 200:
            raise TencentError(f"腾讯行情返回 HTTP {resp.status_code}")

        # 服务端返回 GBK; 不能用 resp.text(httpx 按响应头/utf-8 猜, 中文名会乱码)
        text = resp.content.decode("gbk", errors="replace")
        rows = parse_quotes(text)
        if not rows and "v_" not in text:
            raise TencentError(f"腾讯行情响应结构异常: {text[:120]!r}")
        return rows

    def fetch(self, symbols: list[str]) -> dict[str, list[str]]:
        """分批取快照。批间失败隔离; 全部批次失败时抛 TencentError。"""
        unique: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            unique.append(symbol)

        out: dict[str, list[str]] = {}
        batches = failed = 0
        for i in range(0, len(unique), self._batch_size):
            chunk = unique[i : i + self._batch_size]
            if batches:
                time.sleep(self._interval_s)
            batches += 1
            try:
                out.update(self.fetch_batch(chunk))
            except TencentError as e:
                failed += 1
                logger.warning(
                    "腾讯行情批次失败(%d 只, 第 %d 批): %s", len(chunk), batches, e
                )
        if batches and failed == batches:
            raise TencentError("腾讯行情全部批次失败")
        return out


__all__ = [
    "BASE_URL",
    "BATCH_INTERVAL_S",
    "BATCH_SIZE",
    "TencentClient",
    "TencentError",
    "parse_quotes",
    "to_standard_symbol",
    "to_tencent_code",
]
