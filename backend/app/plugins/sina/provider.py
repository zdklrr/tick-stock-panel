"""新浪行情(免费)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider (service 分流点按这套签名调用), 插件被
loader 扫描注册后, 各 service 无需改动即可路由。

实现数据集:
  - minute      分钟K: 单标的 HTTP 拉取, scale 1/5/15/30/60, 原始不复权价
  - full_minute 全量分钟: 修复轮 get_intraday_batch (当日窗口逐标的)
  - 未实现 get_intraday_latest — 新浪没有全市场批量分钟端点, 服务自动降级为
    「仅修复轮」模式 (盘中节奏下限抬到 60s)
未声明 daily/realtime/depth5/adj_factor/financial → provider_has_dataset 为 False,
对应数据集自动回退 TickFlow。

单位与口径 (CONTRIBUTING §3, 2026-11 实测, 不可凭字段名推断):
  - 分钟 volume 单位「股」 → floor(/100) 转「手」。实测 600000 当日 238 根
    sum(volume)=116422276 股, 与同日日K volume 完全一致, ÷100 = 1164222 手。
  - 分钟 amount 单位「元」 → 原样透传 (实测 371900 股 × 10.065 ≈ 3742919 元)。
  - day 字段已是北京墙钟 "YYYY-MM-DD HH:MM:SS" → strptime 直接得 naive datetime,
    不做任何时区换算 (契约 §get_minute 的 datetime 时区契约)。
  - 价格即接口原值 (接口无复权参数), 天然满足「原始价」基准契约。
  - 历史深度: datalen 上限 1023 根 1m, 实测覆盖 4 个完整交易日 + 第 5 日部分
    (最早一日仅 71 根), 故 minute_history_days = 4 (不虚报 5)。
  - 每交易日 238 根: 09:31-11:30 + 13:01-15:00; 14:58/14:59 属收盘集合竞价,
    上游不单独出分钟, 故不是 240 根。
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.plugins.sina.client import MAX_DATALEN, SinaClient, SinaError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("minute", "full_minute")

# 契约 freq → 上游 scale (实测 scale 支持 1/5/15/30/60)
_FREQ_SCALE = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

_MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}

# 支持的三类标的: 上游分钟接口对个股/ETF/指数同布局 (实测 600000/510300/000001.SH
# 全部返回), 且 volume 一律为「股」(ETF 与指数同样按 /100 折算成手, 已用同日日K
# 总量交叉验证), 故三类都可直接服务, 不因 asset_type 空手而归。
_SUPPORTED_ASSET_TYPES = frozenset({"stock", "etf", "index"})

_BARS_PER_DAY = 238  # 实测 1m 一交易日根数 (14:58/14:59 收盘集合竞价无独立分钟)
_MINUTE_INTERVAL_S = 0.08  # 单标的请求节流 (与 tdx 同档; 实测裸请求约 0.07s)
_BEIJING = ZoneInfo("Asia/Shanghai")


def availability() -> tuple[bool, str]:
    """loader 启动自检: 免费接口无需 Key、无第三方依赖, 恒可用。不抛异常。"""
    return True, ""


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shares_to_lots(value) -> float | None:
    """股 → 手 (向下取整, 与日K同口径); 无法解析返回 None, 不伪造。"""
    v = _to_float(value)
    return None if v is None else float(math.floor(v / 100.0))


def _parse_dt(text) -> datetime | None:
    """上游 day 'YYYY-MM-DD HH:MM:SS' (已是北京墙钟) → naive datetime; 非法返回 None。"""
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _beijing_naive(value: datetime) -> datetime:
    """带时区的窗口归一为北京墙钟 naive; 已是 naive 视为北京墙钟原样返回。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(_BEIJING).replace(tzinfo=None)


def _safe_cb(cb: Callable[[int, int], None] | None, cur: int, total: int) -> None:
    """进度回调是装饰性副作用: 其异常不得中断数据返回。"""
    if cb is None:
        return
    try:
        cb(cur, total)
    except Exception:  # 回调异常不得影响拉取结果
        logger.debug("sina on_chunk_done 回调异常(已忽略)", exc_info=True)


def _minute_rows(
    symbol: str,
    bars: list[dict],
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    today_only=None,
) -> list[dict]:
    """上游分钟数组 → 内部分钟行(datetime 北京墙钟 naive, volume 股→手, amount 元)。

    today_only 非空时仅保留该交易日的行(修复轮用: datalen 尾部天然带前一日余量)。
    """
    out: list[dict] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        dt = _parse_dt(bar.get("day"))
        if dt is None:
            continue
        if today_only is not None and dt.date() != today_only:
            continue
        if start_dt is not None and dt < start_dt:
            continue
        if end_dt is not None and dt > end_dt:
            continue
        close = _to_float(bar.get("close"))
        if close is None:
            continue  # 停牌/无成交分钟, 不伪造
        out.append({
            "symbol": symbol,
            "datetime": dt,
            "open": _to_float(bar.get("open")),
            "high": _to_float(bar.get("high")),
            "low": _to_float(bar.get("low")),
            "close": close,
            "volume": _shares_to_lots(bar.get("volume")),
            "amount": _to_float(bar.get("amount")),
        })
    return out


def _datalen_for_window(start_dt: datetime | None, end_dt: datetime) -> int:
    """窗口 → 单次 datalen (交易日×238 + 余量, 封顶 MAX_DATALEN)。

    start 缺省(全深度请求)直接给上限: 上游只保留约 4.3 个交易日, 多要也不会更多。
    """
    if start_dt is None:
        return MAX_DATALEN
    span_days = max(0, (end_dt.date() - start_dt.date()).days)
    bars = int((span_days + 1) * 5 / 7 * _BARS_PER_DAY) + 2 * _BARS_PER_DAY
    return max(1, min(MAX_DATALEN, bars))


@dataclass
class _SinaConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "sina"
    display_name: str = "新浪行情 (免费)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class SinaProvider:
    """新浪行情数据源。minute = 逐标的分钟K; full_minute = 当日修复轮。"""

    name = "sina"
    builtin = True

    # 浅源声明: datalen 上限 1023 根 1m ≈ 4.3 个交易日 (实测), 前端分时档位据此收窄
    minute_history_days = 4

    def __init__(self) -> None:
        self.config = _SinaConfig()
        self._client: SinaClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> SinaClient:
        if self._client is None:
            self._client = SinaClient()
        return self._client

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        """分钟K → 内部契约(symbol/datetime 北京墙钟 naive/ohlcv/volume 手/amount 元)。

        单标的 HTTP 拉取(datalen 按窗口折算, 封顶 1023)。请求失败抛异常 —
        契约: get_minute 抛异常时上层自动回退 TickFlow 重试。
        个股/ETF/指数均支持 (上游同布局, volume 一律「股」); 未知 asset_type 或
        未知 freq 空手而还 —— 注意上层对空帧不回退, 故白名单按实测能力给全。
        """
        scale = _FREQ_SCALE.get(str(freq or "").strip().lower())
        if not symbols or asset_type not in _SUPPORTED_ASSET_TYPES or scale is None:
            _safe_cb(on_chunk_done, 0, 0)
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        client = self._get_client()
        start_dt = _beijing_naive(start_time) if start_time is not None else None
        end_dt = (
            _beijing_naive(end_time) if end_time is not None
            else datetime.now(_BEIJING).replace(tzinfo=None)
        )
        datalen = _datalen_for_window(start_dt, end_dt)
        total = len(symbols)
        parts: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            # 请求失败应中止历史同步并标记失败, 不得作为无数据继续遍历全市场。
            bars = client.minute_bars(symbol, scale=scale, datalen=datalen)
            rows = _minute_rows(symbol, bars, start_dt, end_dt)
            parts.append(pl.DataFrame(rows, schema=_MINUTE_SCHEMA))
            _safe_cb(on_chunk_done, i + 1, total)
            time.sleep(_MINUTE_INTERVAL_S)
        chunks = [df for df in parts if not df.is_empty()]
        return (
            pl.concat(chunks, how="diagonal_relaxed")
            if chunks else pl.DataFrame(schema=_MINUTE_SCHEMA)
        )

    # ---- full_minute (修复轮) ----
    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 当日窗口逐标的拉取, 只保留今日 1m 行。

        上游无批量端点, 全市场一轮 = 标的数 次请求(含 0.08s 节流, 实测全市场
        约 5500 只一轮 14-20 分钟量级), 由 minute_refresh 以修复轮节奏调度。
        单标的失败软跳过(logger.debug), 整轮失败按空轮返回空 DataFrame。
        """
        if not symbols or asset_type not in _SUPPORTED_ASSET_TYPES:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        client = self._get_client()
        today = datetime.now(_BEIJING).date()
        # 当日 238 根 + count 余量(尾部会带前一日), 封顶上游上限
        fetch = max(_BARS_PER_DAY, min(int(count or 0), MAX_DATALEN - _BARS_PER_DAY))
        datalen = max(1, min(fetch + _BARS_PER_DAY, MAX_DATALEN))
        out: list[pl.DataFrame] = []
        failures = 0
        first_error = ""
        for symbol in symbols:
            try:
                bars = client.minute_bars(symbol, scale=1, datalen=datalen)
            except SinaError as e:
                failures += 1
                first_error = first_error or str(e)
                logger.debug("sina 修复轮 %s 拉取失败: %s", symbol, e)
                time.sleep(_MINUTE_INTERVAL_S)
                continue
            rows = _minute_rows(symbol, bars, today_only=today)
            if rows:
                out.append(pl.DataFrame(rows, schema=_MINUTE_SCHEMA))
            time.sleep(_MINUTE_INTERVAL_S)
        if failures:
            logger.warning(
                "sina 修复轮: %d/%d 只标的失败 (首个错误: %s)",
                failures, len(symbols), first_error,
            )
        return (
            pl.concat(out, how="diagonal_relaxed")
            if out else pl.DataFrame(schema=_MINUTE_SCHEMA)
        )

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset in ("minute", "full_minute"):
            syms = [s for s in (symbols or [])][:3] or ["600000.SH"]
            try:
                if dataset == "full_minute":
                    df = self.get_intraday_batch(syms)
                else:
                    end = datetime.now(_BEIJING).replace(tzinfo=None)
                    # 浅源只有约 4 个交易日, 回看 3 天足够出样本
                    df = self.get_minute(syms, end - timedelta(days=3), end)
            except SinaError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # datetime → ISO 字符串, 保证 JSON 可序列化
                for k, v in list(row.items()):
                    if isinstance(v, datetime):
                        row[k] = v.isoformat()
            return {"provider": self.name, "dataset": dataset, "rows": df.height,
                    "columns": df.columns, "preview": head}
        return {"provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"sina 插件未接入 {dataset} 数据集(自动回退 TickFlow)"}


__all__ = [
    "SinaProvider",
    "availability",
]
