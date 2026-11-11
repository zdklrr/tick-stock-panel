"""标的维表同步服务。

盘前 9:10 调用 tf.exchanges.get_instruments("SH"/"SZ"/"BJ", type="stock")
获取全量标的元数据，flatten ext 字段，写入 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow import client as tf_client
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


def _flatten_instruments(items: list[dict], *, current_limits: bool = True) -> list[dict]:
    """把 SDK 返回的 Instrument 列表 flatten 成扁平行。"""
    rows = []
    for item in items:
        row = {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "code": item.get("code"),
            "exchange": item.get("exchange"),
            "region": item.get("region"),
            "type": item.get("type"),
        }
        ext = item.get("ext") or {}
        row["listing_date"] = ext.get("listing_date")
        row["total_shares"] = ext.get("total_shares")
        row["float_shares"] = ext.get("float_shares")
        row["tick_size"] = ext.get("tick_size")
        row["limit_up"] = ext.get("limit_up") if current_limits else None
        row["limit_down"] = ext.get("limit_down") if current_limits else None
        rows.append(row)
    return rows


def _fetch_instruments_via_provider() -> list[dict] | None:
    """若当前日K数据源不是 tickflow 且该 provider 提供 get_instruments, 用它拉标的维表。

    返回 flatten 行列表; 未命中(仍应走 tickflow)时返回 None。
    标的维表跟随日K数据源(二者天然耦合, 无独立偏好项)。
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider(provider_name):
        return None
    provider = custom_sources.get_provider(provider_name)
    if not hasattr(provider, "get_instruments"):
        return None
    try:
        items = provider.get_instruments("stock") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return None
    rows = _flatten_instruments(items)
    logger.info("instruments via %s: %d stocks", provider_name, len(rows))
    return rows


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    返回写入的行数。
    """
    all_rows = _fetch_instruments_via_provider()
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        tf = get_client()
        # 免费接口的限价来自历史行情，不能盖上同步日期当作当日权威价。
        current_limits = tf_client.current_mode() == "api_key"
        all_rows = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if items:
                    all_rows.extend(_flatten_instruments(items, current_limits=current_limits))
                    logger.info("instruments %s: %d stocks", ex, len(items))
            except Exception as e:
                logger.warning("get_instruments(%s) failed: %s", ex, e)

    if not all_rows:
        return 0

    df = pl.DataFrame(all_rows)
    out = data_dir / "instruments" / "instruments.parquet"

    # 缩表护栏: 免费/降级行情源只返回部分市场 (曾把 5890 行完整维表冲成 2823 行,
    # 且只覆盖沪市 → 深市持仓与选股全部查不到名称/股本)。新结果比现有维表小就
    # 说明源不完整, 直接拒绝写入并保留旧表, 绝不「成功」一次退化。
    existing = _read_existing(out)
    if existing is not None and df.height < existing.height:
        logger.warning(
            "instruments 拒绝缩表: 新源仅 %d 行 < 现有 %d 行 (行情源不完整?), 保留旧维表",
            df.height, existing.height,
        )
        return 0

    df = _preserve_share_fields(df, data_dir)
    # 新源整行缺失的标的 (按 symbol) 从旧维表补回, 不让它们凭空消失
    df = _restore_missing_symbols(df, existing)
    df = df.with_columns(pl.lit(cn_today()).alias("as_of"))

    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def _read_existing(path: Path) -> pl.DataFrame | None:
    """读现有维表 (不存在/损坏返回 None, 不阻断同步)。"""
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 现有维表读取失败: %s", e)
        return None


def _restore_missing_symbols(df: pl.DataFrame, existing: pl.DataFrame | None) -> pl.DataFrame:
    """把新源里整行缺失的 symbol 从旧维表补回 (按 symbol 反连接)。"""
    if existing is None or "symbol" not in df.columns or "symbol" not in existing.columns:
        return df
    try:
        missing = existing.join(df.select("symbol"), on="symbol", how="anti")
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 缺失标的补回失败: %s", e)
        return df
    if missing.is_empty():
        return df
    # 对齐列: 旧表多出的列置 null, 新表多出的列同样补 null
    common = [c for c in df.columns if c in missing.columns]
    try:
        merged = pl.concat([df.select(common), missing.select(common)], how="vertical_relaxed")
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 旧表补回拼接失败: %s", e)
        return df
    logger.info("instruments 从旧维表补回 %d 个缺失标的", missing.height)
    return merged


# 股本与静态交易规则字段: 部分行情源的标的维表只有代码/名称/市场/类型。
# 这些字段缺失会让 enriched 的 turnover_rate / 市值为空, 进而使
# 依赖换手率与流通市值的策略筛不出任何标的 —— 因此绝不能用 null 覆盖已有值。
# name 一并纳入: 免费源偶尔把 name 填成占位符 (等于 code/symbol) 或留空,
# 这里按「缺失」处理并用历史值兜回, 否则持仓/自选/监控全部显示不出中文名。
# 涨跌停价随交易日变化，不从历史维表继承。
_SHARE_FIELDS = (
    "name", "listing_date", "total_shares", "float_shares", "tick_size",
)

#: 视为「name 缺失」的占位值 —— 免费源常用代码自填充, 不能当成有效名称。
_NAME_PLACEHOLDER_COLUMNS = ("name",)


def _missing_mask(df: pl.DataFrame, cols: list[str]):
    """各列「缺失」的并集布尔掩码 (name 把占位符算缺失); 无列返回 None。"""
    mask = None
    for col in cols:
        cond = pl.col(col).is_null()
        if col in _NAME_PLACEHOLDER_COLUMNS and "symbol" in df.columns:
            name = pl.col(col).cast(pl.Utf8, strict=False).str.strip_chars()
            code = pl.col("symbol").cast(pl.Utf8, strict=False).str.split(".").list.first()
            cond = cond | (name == "") | (name == pl.col("symbol").cast(pl.Utf8, strict=False)) | (name == code)
        mask = cond if mask is None else (mask | cond)
    return mask


def _preserve_share_fields(df: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    """用现有维表补全 provider 未提供的股本/交易规则字段 (按 symbol 左连接)。

    新增标的仍保留 null (没有历史值可用, 不伪造); 只在同一 symbol 上把
    provider 的空值替换为既有值, 并在日志里给出补全规模。
    """
    available = [c for c in _SHARE_FIELDS if c in df.columns]
    if not available:
        return df
    # name 的「占位符」(空串 / 等于代码) 也算缺失, 故不能只看 null_count
    missing_mask = _missing_mask(df, available)
    if missing_mask is not None and not bool(df.select(missing_mask.any()).item()):
        return df  # provider 已提供全部字段且无占位符, 无需补全

    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        logger.warning(
            "instruments provider 未提供股本字段且无历史维表可补全: "
            "enriched 换手率/市值将为 null, 依赖它们的策略会筛不出标的"
        )
        return df
    try:
        existing = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("instruments 历史维表读取失败, 无法补全股本字段: %s", e)
        return df
    if "symbol" not in existing.columns:
        return df

    keep = ["symbol", *[c for c in available if c in existing.columns]]
    if len(keep) <= 1:
        return df
    merged = df.join(existing.select(keep), on="symbol", how="left", suffix="_prev")
    filled: list[str] = []
    for col in keep[1:]:
        prev = f"{col}_prev"
        if prev not in merged.columns:
            continue
        # 逐行 coalesce: provider 有值用 provider, 为空才回落历史值。这样
        # 「部分标的缺字段」也能补齐, 而不只处理整列为空的源。
        if col == "name":
            # name 还要把占位符 (空串 / 等于 symbol 或 code) 当成缺失
            bad = (
                pl.col(col).is_null()
                | (pl.col(col).cast(pl.Utf8, strict=False).str.strip_chars() == "")
            )
            if "symbol" in merged.columns:
                code_col = pl.col("symbol").cast(pl.Utf8, strict=False).str.split(".").list.first()
                bad = bad | (
                    pl.col(col).cast(pl.Utf8, strict=False) == pl.col("symbol").cast(pl.Utf8, strict=False)
                ) | (
                    pl.col(col).cast(pl.Utf8, strict=False) == code_col
                )
            if bool(merged.select(bad.any()).item()) and bool(
                merged.select(pl.col(prev).is_not_null().any()).item()
            ):
                merged = merged.with_columns(
                    pl.when(bad).then(pl.col(prev)).otherwise(pl.col(col)).alias(col)
                )
                filled.append(col)
            continue
        if merged[col].null_count() and merged[prev].is_not_null().any():
            merged = merged.with_columns(
                pl.coalesce(pl.col(col), pl.col(prev)).alias(col)
            )
            filled.append(col)
    merged = merged.drop([f"{c}_prev" for c in keep[1:] if f"{c}_prev" in merged.columns])
    if filled:
        restored = int(merged.select(
            [pl.col(c).is_not_null().sum() for c in filled]
        ).row(0)[0])
        logger.info(
            "instruments provider 缺 %s, 已逐行从历史维表补全 (首列非空 %d/%d)",
            "/".join(filled), restored, merged.height,
        )
    return merged


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)


# ── 名称在线兜底 (仅用于展示, 不回写维表) ────────────────────
# 背景: 维表曾被降级源冲成「只剩沪市 2823 行」, 之后的深市持仓在台账里也没有
# name, 于是持仓表只剩代码。维表修复要等下一次全量 sync; 但展示不能等 ——
# 腾讯批快照里每个标的都自带中文名, 按持仓那几十只逐个查即可, 代价可忽略。
_NAME_LOOKUP_TTL_S = 600.0
_NAME_LOOKUP_MAX = 200
_name_lookup_cache: dict[str, tuple[float, str]] = {}
_name_lookup_lock = threading.Lock()


def lookup_names_online(symbols: list[str]) -> dict[str, str]:
    """腾讯批快照按代码查中文名 (展示用兜底, 不回写维表)。

    命中进程内 10 分钟缓存; 网络失败软返回已缓存的部分, 永不抛异常。
    symbols 超过 _NAME_LOOKUP_MAX 时只取前 N 个 (持仓不会到这么多)。
    """
    want = [s for s in dict.fromkeys(symbols) if s][:_NAME_LOOKUP_MAX]
    if not want:
        return {}
    now = time.monotonic()
    out: dict[str, str] = {}
    todo: list[str] = []
    with _name_lookup_lock:
        for sym in want:
            hit = _name_lookup_cache.get(sym)
            if hit is not None and now - hit[0] < _NAME_LOOKUP_TTL_S:
                out[sym] = hit[1]
            else:
                todo.append(sym)
    if not todo:
        return out
    try:
        from app.plugins.tencent.client import TencentClient
        from app.plugins.tencent.provider import _map_row

        client = TencentClient(timeout=6.0)
        try:
            rows = client.fetch(todo)
        finally:
            client.close()
        fetched: dict[str, str] = {}
        now_ms = int(time.time() * 1000)
        for sym, fields in rows.items():
            rec = _map_row(sym, fields, now_ms)
            if not rec:
                continue
            nm = str(rec.get("name") or "").strip()
            if nm and nm != sym and nm != sym.split(".")[0]:
                fetched[sym] = nm
        with _name_lookup_lock:
            for sym, nm in fetched.items():
                _name_lookup_cache[sym] = (now, nm)
        out.update(fetched)
    except Exception as e:  # noqa: BLE001 — 名称兜底失败不影响任何业务
        logger.warning("instruments 在线名称兜底失败: %s", e)
    return out
