"""腾讯行情(qt.gtimg.cn)内置数据源 provider。

实现数据集:
  - realtime   全市场快照: 本地维表(个股 + ETF) ∪ 自选 → 80 只/批批量拉取
  - depth5     同一快照自带的买卖五档 (批量接口, 不额外请求)

字段口径 (按 "~" 分割, 0-based; 2026-11 实测, 个股/ETF/指数/北交所布局一致):
  1=名称  2=代码  3=最新价  4=昨收  5=今开  6=成交量(手)
  9..18  = 买一价,买一量,买二价,买二量,买三价,买三量,买四价,买四量,买五价,买五量
  19..28 = 卖一价,卖一量,卖二价,卖二量,卖三价,卖三量,卖四价,卖四量,卖五价,卖五量
  30=时间 YYYYMMDDHHMMSS(北京墙钟)  31=涨跌额  32=涨跌幅(百分数)
  33=最高  34=最低  36=成交量(手)  37=成交额(万元)
  38=换手率(百分数)  43=振幅(百分数)

单位制 (CONTRIBUTING §3, 不可凭字段名推断):
  - volume 接口已是「手」, 零换算 (不得再 /100)
  - amount 接口为「万元」→ ×10000 转「元」
  - change_pct / turnover_rate / amplitude 接口为「百分数」→ ÷100 转小数制
  - 停牌/无成交 (今开 0 且成交量为 0) 时相关字段返回 None, 不用 0 冒充价格
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ
from app.plugins.tencent.client import TencentClient, TencentError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 未声明的数据集 provider_has_dataset 为 False → 回退 TickFlow
_DATASETS = ("realtime", "depth5")

# 全市场标的池来源: 个股维表 + ETF 维表 (与 jobs.daily_pipeline._resolve_universe 同口径,
# 额外并入 ETF —— 声明 realtime 后全市场轮询里的 ETF 也由本插件出数)
_ETF_DIR = "instruments_etf"
_INDEX_DIR = "instruments_index"

# 维表一天只同步一次; 实时轮询若每轮重读 parquet 纯属白烧 IO, 加 5 分钟缓存
_UNIVERSE_TTL_S = 300.0
_universe_cache: tuple[str, float, list[str]] = ("", 0.0, [])

_REALTIME_FIELDS = (
    "symbol", "name", "last_price", "prev_close", "open", "high", "low",
    "volume", "amount", "change_pct", "change_amount", "amplitude",
    "turnover_rate", "timestamp", "session",
)

_DEPTH_FIELDS = (
    "symbol", "bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp",
)

# 五档: 买盘从索引 9 起, 卖盘从索引 19 起, 每档「价格, 数量」相邻
_DEPTH_LEVELS = 5
_BID_START = 9
_ASK_START = 19


def availability() -> tuple[bool, str]:
    """loader 启动自检: 免费无 Key, 只校验 httpx 可用。不抛异常。"""
    try:
        import httpx  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"httpx 不可用: {e}"
    return True, "ok"


@dataclass
class _TencentConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tencent"
    display_name: str = "腾讯行情 (免费)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _field(fields: list[str], index: int) -> str | None:
    if 0 <= index < len(fields):
        return fields[index]
    return None


def _num(fields: list[str], index: int) -> float | None:
    return _to_float(_field(fields, index))


def _positive(value: float | None) -> float | None:
    """0 / None 都视为「无该价格」(停牌或该档无挂单), 不伪造。"""
    if value is None or value <= 0:
        return None
    return value


def _ratio(fields: list[str], index: int) -> float | None:
    """百分数 → 小数制 (0.40 表示 0.40% → 0.004)。"""
    value = _num(fields, index)
    return value / 100.0 if value is not None else None


def _quote_ms(raw, fallback_ms: int) -> int:
    """字段 30 'YYYYMMDDHHMMSS'(北京墙钟) → epoch 毫秒; 解析失败退回本地时间。"""
    text = str(raw or "").strip()
    if len(text) < 14 or not text[:14].isdigit():
        return fallback_ms
    try:
        stamp = datetime(
            int(text[0:4]), int(text[4:6]), int(text[6:8]),
            int(text[8:10]), int(text[10:12]), int(text[12:14]),
            tzinfo=CN_TZ,
        )
    except ValueError:
        return fallback_ms
    return int(stamp.timestamp() * 1000)


def _map_row(symbol: str, fields: list[str], fallback_ms: int) -> dict | None:
    """腾讯快照字段数组 → 内部 realtime record。缺字段返回 None, 不启发式补全。"""
    last = _positive(_num(fields, 3))
    prev = _positive(_num(fields, 4))
    if last is None and prev is None:
        return None

    raw_open = _num(fields, 5)
    raw_high = _num(fields, 33)
    raw_low = _num(fields, 34)
    raw_volume = _num(fields, 6)
    if raw_volume is None:
        raw_volume = _num(fields, 36)

    # 停牌 / 尚未产生成交: 今开为 0 且成交量为 0 → 当日 OHLCV 与涨跌幅均无意义,
    # 返回 None 而不是 0 (0 会被下游当作有效价格写进日K)
    halted = not raw_open and not raw_volume

    if halted:
        open_ = high = low = volume = amount = None
        change_amount = change_pct = amplitude = turnover_rate = None
    else:
        open_ = _positive(raw_open)
        high = _positive(raw_high)
        low = _positive(raw_low)
        volume = math.floor(raw_volume) if raw_volume is not None else None
        raw_amount = _num(fields, 37)
        amount = raw_amount * 10_000.0 if raw_amount else None
        # 涨跌额/涨跌幅优先用服务端字段(31/32), 缺失时按 change_amount/prev_close 推导
        change_amount = _num(fields, 31)
        if change_amount is None and last is not None and prev is not None:
            change_amount = last - prev
        change_pct = _ratio(fields, 32)
        if change_pct is None and change_amount is not None and prev:
            change_pct = change_amount / prev  # 小数制, 不乘 100
        amplitude = _ratio(fields, 43)
        turnover_rate = _ratio(fields, 38)

    name = (_field(fields, 1) or "").strip() or None
    return {
        "symbol": symbol,
        "name": name,
        "last_price": last,
        "prev_close": prev,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": amplitude,
        "turnover_rate": turnover_rate,
        "timestamp": _quote_ms(_field(fields, 30), fallback_ms),
        "session": None,
    }


def _book(fields: list[str], start: int) -> tuple[list[float], list[int]]:
    """解析一侧五档: 价格为 0 的档位剔除(该档无挂单), 数量单位手。"""
    prices: list[float] = []
    volumes: list[int] = []
    for level in range(_DEPTH_LEVELS):
        price = _positive(_num(fields, start + level * 2))
        if price is None:
            continue
        volume = _num(fields, start + level * 2 + 1)
        prices.append(price)
        volumes.append(math.floor(volume) if volume and volume > 0 else 0)
    return prices, volumes


def _depth_entry(fields: list[str], fallback_ms: int) -> dict | None:
    """快照五档 → 标准盘口字典; 买/卖任一为空则不提供该标的(不伪造空档)。"""
    bid_prices, bid_volumes = _book(fields, _BID_START)
    ask_prices, ask_volumes = _book(fields, _ASK_START)
    if not bid_prices or not ask_prices:
        return None
    return {
        "bid_prices": bid_prices,
        "bid_volumes": bid_volumes,
        "ask_prices": ask_prices,
        "ask_volumes": ask_volumes,
        "timestamp": _quote_ms(_field(fields, 30), fallback_ms),
    }


def _read_symbols(path: Path) -> list[str]:
    try:
        df = pl.read_parquet(path, columns=["symbol"])
    except Exception as e:  # noqa: BLE001
        logger.debug("腾讯行情: 读取标的维表 %s 失败: %s", path, e)
        return []
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return [s for s in df["symbol"].to_list() if s]


def _read_dim(directory: Path, canonical: str) -> list[str]:
    """读一个维表目录: 优先规范单文件, 缺失时回退遍历(兼容旧版分片布局)。"""
    path = directory / canonical
    if path.exists():
        return _read_symbols(path)
    out: list[str] = []
    for part in sorted(directory.glob("**/*.parquet")):
        out.extend(_read_symbols(part))
    return out


def _index_symbols(data_dir: Path) -> tuple[set[str], set[str]]:
    """指数维表 → (指数 symbols, 早期混写在指数表里的 ETF symbols)。

    与 repository._refresh_etf_instruments 的兼容口径一致: 早期版本把 ETF 行也写在
    instruments_index 里(asset_type=="etf"), 这些要当 ETF 保留, 不能随指数一起剔除。
    无 asset_type 列时无法区分, 一律按指数处理。
    """
    indexes: set[str] = set()
    legacy_etfs: set[str] = set()
    directory = data_dir / _INDEX_DIR
    path = directory / "instruments_index.parquet"
    parts = [path] if path.exists() else sorted(directory.glob("**/*.parquet"))
    for part in parts:
        try:
            df = pl.read_parquet(part)
        except Exception as e:  # noqa: BLE001
            logger.debug("腾讯行情: 读取指数维表 %s 失败: %s", part, e)
            continue
        if df.is_empty() or "symbol" not in df.columns:
            continue
        if "asset_type" in df.columns:
            legacy_etfs.update(
                s for s in df.filter(pl.col("asset_type") == "etf")["symbol"].to_list() if s
            )
            df = df.filter(pl.col("asset_type") != "etf")
        indexes.update(s for s in df["symbol"].to_list() if s)
    return indexes, legacy_etfs


def _build_universe() -> list[str]:
    """全市场标的池 = 个股维表 ∪ ETF 维表 ∪ 自选 − 指数维表。

    与 jobs.daily_pipeline._resolve_universe 同口径(本地维表 + 自选, 单一来源,
    维表缺失时宁缺毋假), 额外并入 ETF 维表以覆盖 ETF 实时行情, 并剔除指数
    (指数走 get_realtime_indices, 进股票池会污染 kline_daily)。
    """
    from app.config import settings

    data_dir = Path(settings.data_dir)
    base: set[str] = set()
    base.update(_read_dim(data_dir / "instruments", "instruments.parquet"))
    base.update(_read_dim(data_dir / _ETF_DIR, "instruments_etf.parquet"))

    try:
        from app.tickflow.pools import get_pool

        base.update(get_pool("watchlist"))
    except Exception as e:  # noqa: BLE001
        logger.debug("腾讯行情: 自选池读取失败, 仅用维表: %s", e)
        base.update(_read_symbols(data_dir / "user_data" / "watchlist.parquet"))

    indexes, legacy_etfs = _index_symbols(data_dir)
    base.update(legacy_etfs)  # 旧布局里 ETF 混在指数表, 不能随指数一起剔掉
    base -= indexes
    return sorted(s for s in base if s)


def _universe_symbols() -> list[str]:
    """带 TTL 缓存的标的池(维表一天只变一次, 实时轮询不必每轮重读 parquet)。

    缓存按 data_dir 键控: 切库(含测试用 tmp 目录)后必须立刻重算, 否则会把上一个
    库的标的池带过来 —— 那既让测试互相污染, 真机上改数据目录后也会用错池子。
    """
    global _universe_cache
    from app.config import settings

    key = str(settings.data_dir)
    now = time.monotonic()
    cached_key, cached_at, cached = _universe_cache
    if cached_key == key and cached and now - cached_at < _UNIVERSE_TTL_S:
        return cached
    symbols = _build_universe()
    if symbols:  # 空池不缓存: 维表尚未同步时, 下一轮同步完成后可立即恢复
        _universe_cache = (key, now, symbols)
    return symbols


class TencentProvider:
    """腾讯行情数据源。realtime = 全市场批量快照; depth5 复用同一快照的买卖五档。"""

    name = "tencent"
    builtin = True

    def __init__(self) -> None:
        self.config = _TencentConfig()
        self._client: TencentClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TencentClient:
        if self._client is None:
            self._client = TencentClient()
        return self._client

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            universe = _universe_symbols()
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯行情: 全市场标的池解析失败: %s", e)
            return []
        if not universe:
            logger.warning("腾讯行情: 全市场标的池为空(维表与自选均无数据), 本轮跳过")
            return []

        fallback_ms = int(time.time() * 1000)
        try:
            rows = self._get_client().fetch(universe)
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯行情: 全市场快照拉取失败(请求 %d 只): %s", len(universe), e)
            return []

        records: list[dict] = []
        dropped = 0
        for symbol, fields in rows.items():
            record = _map_row(symbol, fields, fallback_ms)
            if record is None:
                dropped += 1
                continue
            records.append(record)
        if not records:
            logger.warning(
                "腾讯行情: 本轮 0 条(请求 %d 只, 返回 %d 行), 疑似接口结构变化",
                len(universe), len(rows),
            )
        elif dropped:
            logger.info("腾讯行情拉取完成: %d 条(丢弃 %d 行无价快照)", len(records), dropped)
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照(可选插件协议, quote_service 鸭子类型调用)。

        腾讯快照直接支持指数代码; 未映射/无返回的代码跳过(上层用本地日K兜底)。
        失败返回 None 保留上轮有效缓存, 成功但无数据返回 []。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for symbol in symbols or []:
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            wanted.append(symbol)
        if not wanted:
            return []

        fallback_ms = int(time.time() * 1000)
        try:
            rows = self._get_client().fetch(wanted)
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯行情: 指数快照拉取失败: %s", e)
            return None

        records: list[dict] = []
        for symbol, fields in rows.items():
            record = _map_row(symbol, fields, fallback_ms)
            if record is not None:
                records.append(record)
        logger.info("腾讯行情指数拉取完成: %d 条(请求 %d 只)", len(records), len(wanted))
        return records

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口 → {symbol: 标准盘口字典}。数量单位手, 价格为 0 的档位剔除。"""
        if not symbols:
            return {}
        fallback_ms = int(time.time() * 1000)
        try:
            rows = self._get_client().fetch(symbols)
        except Exception as e:  # noqa: BLE001
            logger.warning("腾讯行情: 五档快照拉取失败: %s", e)
            return {}

        out: dict[str, dict] = {}
        for symbol, fields in rows.items():
            entry = _depth_entry(fields, fallback_ms)
            if entry is not None:
                out[symbol] = entry
        return out

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "error": f"腾讯插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        wanted = [s for s in (symbols or [])][:5] or ["600000.SH", "000001.SZ"]
        try:
            rows = self._get_client().fetch(wanted)
        except TencentError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}

        fallback_ms = int(time.time() * 1000)
        if dataset == "realtime":
            head = [
                r for r in (_map_row(s, f, fallback_ms) for s, f in rows.items()) if r
            ]
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": len(head),
                "columns": list(head[0].keys()) if head else list(_REALTIME_FIELDS),
                "preview": head[:5],
            }

        depths = {
            s: d
            for s, d in ((s, _depth_entry(f, fallback_ms)) for s, f in rows.items())
            if d
        }
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(depths),
            "columns": list(_DEPTH_FIELDS),
            "preview": [{"symbol": s, **d} for s, d in list(depths.items())[:5]],
        }


__all__ = ["TencentProvider", "availability"]
