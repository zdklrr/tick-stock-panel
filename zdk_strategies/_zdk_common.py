"""zdk 选股策略移植公共库（TSP 自定义策略辅助模块）。

本文件名以下划线开头，TSP 策略加载器不会把它当成策略（加载器只扫非下划线
开头的 .py），但同目录的策略文件可以 ``import _zdk_common`` —— TSP 加载策略
时会把 data/strategies/custom 加入 sys.path，并把同目录 `_*.py` 一并做 AST
安全校验。

设计目标：**逐条复刻 zdk 选股策略软件的判定口径**，只做必要的字段映射。

字段映射（zdk → TSP enriched 列）
--------------------------------
price             → close                        元
pct (%)           → change_pct                   TSP 是小数 → ×100
d5/d10/d20/d60(%) → momentum_5d/10d/20d/60d      TSP 是小数 → ×100
hsl (%)           → turnover_rate                同为百分数，直接用
turnover (万元)   → amount                       TSP 是元 → ×10000
zf (%)            → amplitude                    TSP 是小数 → ×100
lb (量比)         → vol_ratio_5d                 前 5 日均量(不含当日)
floatcap (亿)     → close*float_shares / 1e8
涨停判定          → consecutive_limit_ups        TSP 由涨跌停判定价算出，比按
                                                 「涨幅≈10%」猜更准（覆盖 ST 5%、
                                                 创业板/科创板 20%、北交所 30%）
距涨停 (%)        → (limit_up_price/close-1)*100   limit_up_price 已含容差

zdk 里有、TSP 没有的数据
------------------------
main / main_ratio / main5_net / main5_ratio   主力资金净流入（需付费扩展源）
pe                                            市盈率（财务数据未 JOIN 到 enriched）
rb                                            行业板块成分（心法框架引擎用）

策略文件对这三类字段一律「剔除后按剩余权重重新归一化」，并在 RULES 里写明。
唯一例外是 zdk 的「主力资金」策略 —— 它 65% 权重来自资金流，剔除后已不成
策略，故不移植（见 README.md）。

实现注意（polars 语义）
-----------------------
``.over("symbol")`` 会对整个前面的表达式按标的分组求值，因此本模块**绝不**把
一个带 ``.over()`` 的表达式再嵌进另一个 ``.over()`` 里（polars 会报
nested window）。派生列一律拆成多个顺序 ``with_columns`` 批次：先落下单行的
逐元素列，再引用已物化的列做滚动/窗口计算。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any

import polars as pl

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 热度/涨停类策略的回看窗口（需要「近 60 日新高」「近 20 日涨停次数」）。
HOT_LOOKBACK_DAYS = 70

#: 趋势类策略的回看窗口（最长 60 日动量 + 60 日均线）。
TREND_LOOKBACK_DAYS = 70

#: 「涨停形态」类策略的回看深度（zdk limit_pattern 里为 20）。
LIMIT_PATTERN_LOOKBACK = 20

#: zdk ``screener.DEFAULT_PARAMS`` 里与「可交易性」相关的默认值（单位对齐 zdk 侧）。
#: 这些条件在 zdk 里位于 ``apply_prefilter`` 的最前面、**早于横截面打分**，
#: 因此必须同样在打分之前执行 —— 否则百分位排名的分母与 zdk 不一致，
#: 同一只票算出来的分数和名次都会漂移。
UNIVERSE_DEFAULTS: dict[str, float | bool] = {
    "min_price": 3.0,        # 元
    "max_price": 300.0,      # 元
    "min_turnover": 1.0,     # 亿元 → amount >= 1e8 元
    "min_floatcap": 30.0,    # 亿元
    "max_floatcap": 0.0,     # 亿元，0 = 不限
    "exclude_st": True,
    "exclude_limit": True,   # 剔除 |涨幅| >= 9.5%（买不进 / 卖不掉）
    "candidate_pool": 200,   # 进入技术面确认的候选数量
}

#: 所有策略共同依赖的 enriched 列（显式声明，避免回测层退化为全量指标计算）。
ZDK_FEATURES = frozenset({
    "close", "open", "high", "low", "volume", "amount", "prev_close",
    "turnover_rate", "change_pct", "amplitude",
    "ma5", "ma10", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "rsi_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_60d",
    "vol_ratio_5d",
    "float_shares", "name",
})

#: zdk 有、TSP 无对应数据的 prefilter 键（剔除，不做静默近似替换）。
UNSUPPORTED_PREFILTER: tuple[str, ...] = ("main_min", "main5_min", "pe_min", "pe_max")

#: 同上：权重键。剔除后按剩余权重重新归一化。
UNSUPPORTED_WEIGHTS: frozenset[str] = frozenset({
    "main_ratio", "main5_ratio", "main5_net", "pe_low",
})


# ---------------------------------------------------------------------------
# 第 1 批：逐元素派生列 + 单次 over 的「前一日」列
# ---------------------------------------------------------------------------

def _prev(column: str) -> pl.Expr:
    """同标的上一个交易日的值。"""
    return pl.col(column).shift(1).over("symbol")


#: 涨停判定的涨幅容差（百分点）。仅在拿不到精确涨停价时用作退路。
BOARD_TOUCH_TOL = 0.40

#: 交易所生效价容差：``raw_close >= limit_up_price`` 即涨停（TSP 口径）。
#: 定义在 ``backend/app/price_limits.py``（pipeline 用整数分算术算出来）。
_EXCHANGE_PRICE_TOL = 0.005

#: 主板 ST 涨跌幅由 5% 改为 10% 的生效日。对齐 TSP
#: ``app.price_limits.MAIN_BOARD_ST_LIMIT_CHANGE_DATE``。
MAIN_BOARD_ST_LIMIT_CHANGE_DATE = _date(2026, 7, 6)

#: 创业板注册制改革日：此前创业板与主板同为 ±10%。
GEM_20PCT_LIMIT_START_DATE = _date(2020, 8, 24)


def _limit_pct_expr() -> pl.Expr:
    """按交易所规则推算当日涨跌幅限制（小数，0.10 = 10%）。

    逐条对齐 TSP 的单一事实源 :func:`app.price_limits.polars_price_limit_pct`
    （``backend/app/price_limits.py:140-172``），**不照抄 zdk**：zdk 的
    ``_limit_pct_series`` 有两处与 2026 年的真实规则不符，直接搬过来会造成
    系统性误判（实测 2026-11-02 全市场把 146 只判成涨停，而交易所口径只有 59 只）：

    1. **北交所按 ``.BJ`` 后缀识别**，不是按代码首位。北交所现有 920xxx
       代码（实测 354 只），zdk 的 ``8``/``4`` 开头规则一只都匹配不到，
       会把这些股票当成 10% 主板，其 30% 涨停全部漏判。
    2. **主板 ST 自 2026-07-06 起是 10%，不再是 5%**。zdk 无条件按 5%
       处理 ST，导致 ``*ST西旅`` 涨 8.81% 被误判为涨停（真实涨停是 10%）。
       创业板/科创板的 ST 一直是 20%（``legacy_main_st`` 只作用于主板）。
    3. 创业板前缀要含 ``302``（zdk 只写了 300/301/688/689）。

    创业板 2020-08-24 之前按 10% 处理的回溯逻辑也一并保留，回测早期数据才正确。
    """
    sym = pl.col("symbol").cast(pl.Utf8)
    trade_date = pl.col("date")
    is_growth = (
        sym.str.starts_with("300") | sym.str.starts_with("301")
        | sym.str.starts_with("302")
    )
    is_star = sym.str.starts_with("688") | sym.str.starts_with("689")
    is_bj = sym.str.ends_with(".BJ")
    gem_pre_registration = (
        is_growth & ~is_bj & (trade_date < pl.lit(GEM_20PCT_LIMIT_START_DATE))
    )
    is_st = pl.col("name").fill_null("").str.to_uppercase().str.contains("ST")
    # 主板 ST 才降档；创业板/科创板/北交所的 ST 不降档。
    legacy_main_st = (
        is_st.fill_null(False)
        & ~(is_growth | is_star | is_bj)
        & (trade_date < pl.lit(MAIN_BOARD_ST_LIMIT_CHANGE_DATE))
    )
    return (
        pl.when(gem_pre_registration).then(0.10)
        .when(is_growth | is_star).then(0.20)
        .when(is_bj).then(0.30)
        .when(legacy_main_st).then(0.05)
        .otherwise(0.10)
        .cast(pl.Float64)
    )


def _limit_price_expr(limit_pct: pl.Expr, *, up: bool) -> pl.Expr:
    """交易所涨停价，整数分算术（对齐 ``app.price_limits.polars_limit_price``）。

    ``((cents * numerator + 50) // 100) / 100`` 是四舍五入到分的标准做法，
    用整数避免浮点误差 —— 判涨停只要差 1 分就会错一整只票。
    """
    sign = 1 if up else -1
    numerator = ((1 + sign * limit_pct) * 100).round(0).cast(pl.Int64)
    cents = (pl.col("prev_close") * 100 + 0.5).floor().cast(pl.Int64)
    return ((cents * numerator + 50) // 100) / 100.0


def _base_masks() -> list[pl.Expr]:
    """涨停相关的基础「逐元素」掩码（全部只用当日数据，无未来函数）。

    **为什么不用 TSP 的 ``limit_up_price`` / ``consecutive_limit_ups``**：
    这两列只由 ``compute_limit_signals`` 产出，而 ``ScreenerService`` 命中
    repo 预计算缓存（``get_enriched_history``，启动时按 300 天窗口预热）时
    帧里**没有**它们 —— 同一条策略在「缓存命中」与「慢路径」两种数据来源下
    会得到不同结果（实测缓存 68 列 vs 慢路径 76 列）。所以这里按交易所
    规则自行推算涨停价，保证实盘、缓存、回测三条路径完全一致。

    判定口径（与 TSP pipeline 的 ``limit_up_price`` 同源）：

    * ``涨停价`` = 四舍五入到分的 ``prev_close × (1 + 涨跌幅)``
    * ``is_lu`` = ``close >= 涨停价 - 0.005``（交易所生效价容差）
    * ``is_ld`` = ``close <= 跌停价 + 0.005``
    * ``touched`` = ``high >= 涨停价 - 0.005``（盘中触板）
    * ``one_word`` = ``low >= 涨停价 - 0.005``（一字板，排板买不到）
    * ``openable`` = ``open < 涨停价 - 0.005``（开盘未封板，可进场）

    与 zdk 原实现的关键差异：zdk 用「涨幅 ≥ 涨停幅度 − 0.40pp」这种**开区间
    近似**，在 TSP 数据上会把 ST 股的 8.81% 误判成涨停（见 ``_limit_pct_expr``
    的说明）。改用精确涨停价后，判定与交易所口径逐只一致。
    """
    limit_pct = _limit_pct_expr()
    close, high, low, open_ = (
        pl.col("close"), pl.col("high"),
        pl.col("low"), pl.col("open"),
    )
    prev = pl.col("prev_close")
    valid = prev.is_not_null() & (prev > 0)
    up_price = _limit_price_expr(limit_pct, up=True)
    down_price = _limit_price_expr(limit_pct, up=False)
    up_bar = up_price - _EXCHANGE_PRICE_TOL
    # 百分比涨幅仍按 zdk 口径保留（用于前端展示与 dist 类因子）
    chg = (close / prev - 1.0) * 100.0
    return [
        (valid & (close >= up_bar)).fill_null(False).alias("zdk_is_lu"),
        (valid & (close <= down_price + _EXCHANGE_PRICE_TOL))
        .fill_null(False).alias("zdk_is_ld"),
        (limit_pct * 100.0).alias("zdk_limit_pct"),
        up_price.alias("zdk_limit_up_est"),
        (pl.col("change_pct") * 100.0).alias("zdk_pct"),
        (pl.col("amplitude") * 100.0).alias("zdk_zf"),
        pl.col("vol_ratio_5d").alias("zdk_lb"),
        (close * pl.col("float_shares") / 1e8).alias("zdk_floatcap"),
        ((close / pl.col("ma5") - 1.0) * 100.0).alias("zdk_dist_ma5"),
        ((close / pl.col("ma10") - 1.0) * 100.0).alias("zdk_dist_ma10"),
        ((close / pl.col("ma20") - 1.0) * 100.0).alias("zdk_dist_ma20"),
        (
            pl.when(valid & (close > 0))
            .then((up_price / close - 1.0) * 100.0)
            .otherwise(None)
            .clip(-50.0, 50.0)
        ).alias("zdk_dist_to_limit"),
        (close > pl.col("ma20")).alias("zdk_above_ma20"),
        (close > pl.col("ma60")).alias("zdk_above_ma60"),
        (close > pl.col("ma10")).alias("zdk_above_ma10"),
        (
            (close > pl.col("ma20"))
            & (pl.col("ma5") > pl.col("ma10"))
            & (pl.col("ma10") > pl.col("ma20"))
        ).alias("zdk_bull_stack"),
        (((close / pl.col("ma20") - 1.0)).abs() <= 0.03).alias("zdk_pullback_ma20"),
        (pl.col("momentum_20d") * 100.0).alias("zdk_roc20"),
        # 供第二批派生列复用的中间量（价格口径，不是百分比）
        up_bar.alias("_zdk_up_bar"),
        (down_price + _EXCHANGE_PRICE_TOL).alias("_zdk_dn_bar"),
        up_price.alias("_zdk_up_price"),
    ]


def _post_masks() -> list[pl.Expr]:
    """第二批：依赖 ``zdk_is_lu`` / ``_zdk_grp`` 已物化列的形态掩码与连板数。"""
    is_lu = pl.col("zdk_is_lu").fill_null(False)
    is_int = is_lu.cast(pl.Int32)
    # 连板数 = 段内 is_lu 的累计和。``_zdk_grp`` 在每个「非涨停日」自增，
    # 故同一段内只有涨停日才会计数，得 1,2,3...；非涨停日自身即段首、值为 0。
    # 只含一次 .over，不触发 nested window。
    streak = is_int.cum_sum().over(["symbol", "_zdk_grp"])
    up_bar = pl.col("_zdk_up_bar")
    return [
        streak.fill_null(0).cast(pl.Int32).alias("zdk_streak"),
        # 一字板：最低价也在涨停价上（含容差），排板也买不到
        (is_lu & (pl.col("low") >= up_bar)).fill_null(False)
        .alias("zdk_one_word"),
        # 可进场：开盘价低于涨停价，集合竞价能买到
        (is_lu & (pl.col("open") < up_bar)).fill_null(False)
        .alias("zdk_openable"),
        # 盘中触板（最高价摸到涨停价）
        (pl.col("high") >= up_bar).fill_null(False).alias("zdk_touched"),
    ]


def _prev_columns() -> list[pl.Expr]:
    """「昨日对应值」列（每个表达式只含一次 ``.over``）。"""
    return [
        _prev("change_pct").mul(100.0).alias("zdk_prev_pct"),
        _prev("open").alias("zdk_prev_open"),
        _prev("macd_dif").alias("zdk_prev_dif"),
        _prev("macd_dea").alias("zdk_prev_dea"),
    ]


def _rolling_columns() -> list[pl.Expr]:
    """引用已物化布尔列的滚动/窗口列。"""
    is_lu = pl.col("zdk_is_lu").cast(pl.Int32)
    return [
        is_lu.shift(1).over("symbol").fill_null(0).alias("zdk_prev_lu"),
        pl.col("zdk_is_ld").cast(pl.Int32).shift(1).over("symbol").fill_null(0)
          .alias("zdk_prev_ld"),
        pl.col("zdk_broken").cast(pl.Int32).shift(1).over("symbol").fill_null(0)
          .alias("zdk_prev_broken"),
        pl.col("zdk_streak").shift(1).over("symbol").fill_null(0)
          .alias("zdk_prev_streak"),
        is_lu.rolling_sum(5, min_samples=1).over("symbol").alias("zdk_lu_5"),
        is_lu.rolling_sum(10, min_samples=1).over("symbol").alias("zdk_lu_10"),
        is_lu.rolling_sum(20, min_samples=1).over("symbol").alias("zdk_lu_20"),
        # 近 20 日是否出现过一字板（zdk framework 第 6 条限制）。
        pl.col("zdk_one_word").cast(pl.Int32)
          .rolling_max(20, min_samples=1).over("symbol")
          .fill_null(0).cast(pl.Boolean).alias("zdk_ow_20"),
        is_lu.rolling_sum(3, min_samples=1).shift(1).over("symbol")
          .fill_null(0).alias("zdk_lu_3_prev"),
        # 前 60 个交易日最高价（排除当日，最高价口径）。
        # zdk：high.rolling(60, min_periods=20).max().shift(1)
        pl.col("high").shift(1).rolling_max(60, min_samples=20).over("symbol")
          .alias("zdk_high60_prev"),
        # 创 20 / 60 日新高（最高价口径，窗口排除当日）
        (
            pl.col("high")
            >= pl.col("high").shift(1).rolling_max(20, min_samples=20).over("symbol")
        ).fill_null(False).alias("zdk_new_high20"),
        (
            pl.col("high")
            >= pl.col("high").shift(1).rolling_max(60, min_samples=20).over("symbol")
        ).fill_null(False).alias("zdk_new_high60"),
    ]


def _ensure_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """补齐派生列计算所需的源列。

    ``turnover_rate`` 是 TSP 的存储列，但**只有慢路径**（scan_parquet）会带上；
    ``ScreenerService`` 命中 repo 预计算缓存时帧里没有它（实测缓存 68 列，
    无 ``turnover_rate`` / ``limit_up_price`` / ``consecutive_limit_*``）。
    为让同一策略在两条数据来源下结果一致，这里按 TSP 自己的定义补算::

        turnover_rate = volume(手) × 100(股/手) / float_shares × 100(%)

    该等式已用真实数据验证：``float_shares == volume × 10000 / turnover_rate``
    在 5216 个有效样本上逐条成立（比值恒为 1.0）。北交所标的没有
    ``float_shares`` / ``turnover_rate``，补出来仍是 null，交由下游
    ``apply_universe`` 的流通市值门槛自然剔除。
    """
    if "turnover_rate" in frame.columns:
        return frame
    if "float_shares" not in frame.columns:
        return frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("turnover_rate"))
    return frame.with_columns(
        pl.when(pl.col("float_shares").is_not_null() & (pl.col("float_shares") > 0))
        .then(pl.col("volume") * 10000.0 / pl.col("float_shares"))
        .otherwise(None)
        .alias("turnover_rate")
    )


def add_derived(frame: pl.DataFrame) -> pl.DataFrame:
    """注入 zdk 口径的派生列。

    分六批落列，避免 polars 的 nested window 限制：
    ⓪补列（``turnover_rate`` 在缓存历史帧里不存在）①逐元素列（含涨停判定
    的中间量）②分段标识 ③依赖 zdk_is_lu 的形态与连板数 ④破板标记
    ⑤前一日列 ⑥滚动列 ⑦依赖⑥的 MACD 金叉。
    """
    work = _ensure_columns(frame)
    work = work.with_columns(_base_masks())
    # 连板计数用的分段标识：每个「非涨停日」开启新段。必须单独一批 —— 同一批
    # with_columns 内不能引用本批新生成的 zdk_is_lu。
    work = work.with_columns(
        (~pl.col("zdk_is_lu").fill_null(False)).cast(pl.Int32)
        .cum_sum().over("symbol").alias("_zdk_grp")
    )
    work = work.with_columns(_post_masks())
    # 破板/烂板：盘中触板但没封住
    work = work.with_columns(
        (pl.col("zdk_touched") & ~pl.col("zdk_is_lu")).alias("zdk_broken")
    )
    work = work.with_columns(_prev_columns())
    work = work.with_columns(_rolling_columns())
    # MACD 金叉（近 3 根内 dif 上穿 dea）。用已物化的前一日列，避免嵌套 over。
    cross = (
        (pl.col("macd_dif") > pl.col("macd_dea"))
        & (pl.col("zdk_prev_dif") <= pl.col("zdk_prev_dea"))
    )
    work = work.with_columns(
        cross.cast(pl.Int32).rolling_max(3, min_samples=1).over("symbol")
        .fill_null(0).cast(pl.Boolean).alias("zdk_macd_gold")
    )
    # 中间量用完即弃，避免污染策略输出列（_zdk_grp 是连板计数的分段标识）
    return work.drop(["_zdk_up_bar", "_zdk_dn_bar", "_zdk_up_price", "_zdk_grp"])


# ---------------------------------------------------------------------------
# 涨停形态（zdk indicators.limit_pattern：涨停 + 回调 / 横盘）
# ---------------------------------------------------------------------------

def _limit_pattern_columns(lookback: int) -> list[pl.Expr]:
    """构造 limit_pattern 的判定列。

    ``k`` = 最近一次涨停发生在 k 个交易日之前（1..lookback）。zdk 是 pandas
    逐行向下扫描取**最近**一次；这里用 when 链向量化 —— 从大 k 往小 k 依次
    覆盖，最后一个命中的（最小的 k）胜出，即「距今最近的那次涨停」。

    每个分支内部的 ``.over("symbol")`` 作用在**整条子表达式**上，故不会出现
    nested window：``shift(k).rolling_mean(5).over("symbol")`` 是「先按标的
    shift、再按标的滚动」，正是 zdk 的逐股口径。
    """
    missing = pl.lit(lookback + 1, dtype=pl.Int64)

    days = missing
    limit_vol: pl.Expr = pl.lit(None, dtype=pl.Float64)
    base_vol: pl.Expr = pl.lit(None, dtype=pl.Float64)
    limit_close: pl.Expr = pl.lit(None, dtype=pl.Float64)
    after_min: pl.Expr = pl.lit(None, dtype=pl.Float64)

    for k in range(lookback, 0, -1):
        hit = pl.col("zdk_is_lu").shift(k).over("symbol").fill_null(False)
        days = pl.when(hit).then(pl.lit(k, dtype=pl.Int64)).otherwise(days)

    for k in range(1, lookback + 1):
        is_k = days == k
        # 涨停日当天的成交量 / 收盘价
        limit_vol = pl.when(is_k).then(
            pl.col("volume").shift(k).over("symbol")
        ).otherwise(limit_vol)
        limit_close = pl.when(is_k).then(
            pl.col("close").shift(k).over("symbol")
        ).otherwise(limit_close)
        # 涨停日之前 5 个交易日的均量（zdk: vol[max(0,idx-5):idx].mean()）
        base_vol = pl.when(is_k).then(
            pl.col("volume").shift(k + 1).rolling_mean(5, min_samples=1).over("symbol")
        ).otherwise(base_vol)
        # 涨停「之后」到今日的最小单日量 = 最近 k 根（含今日、不含涨停日）的滚动最小
        after_min = pl.when(is_k).then(
            pl.col("volume").rolling_min(k).over("symbol")
        ).otherwise(after_min)

    day_ratio = (
        pl.when(
            limit_vol.is_not_null() & (limit_vol > 0)
            & base_vol.is_not_null() & (base_vol > 0)
        )
        .then(limit_vol / base_vol)
        .otherwise(0.0)
    )
    pullback_pct = (
        pl.when(limit_close.is_not_null() & (limit_close > 0))
        .then((pl.col("close") / limit_close - 1.0) * 100.0)
        .otherwise(0.0)
    )
    min_vol_ratio = (
        pl.when(
            limit_vol.is_not_null() & (limit_vol > 0) & after_min.is_not_null()
        )
        .then(after_min / limit_vol)
        .otherwise(1.0)
    )
    return [
        days.alias("zdk_lp_days"),
        (days <= lookback).fill_null(False).alias("zdk_lp_ok"),
        day_ratio.fill_null(0.0).alias("zdk_lp_limit_vol_ratio"),
        pullback_pct.fill_null(0.0).alias("zdk_lp_pullback_pct"),
        min_vol_ratio.fill_null(1.0).alias("zdk_lp_min_vol_ratio"),
    ]


def add_limit_pattern(frame: pl.DataFrame, lookback: int = LIMIT_PATTERN_LOOKBACK) -> pl.DataFrame:
    """追加「最近一次涨停距今天数 / 放量倍数 / 回调幅度 / 缩量程度」列。

    仅在涨停回马枪 / 涨停横盘两条策略里调用 —— 对用不到的策略不必付这个成本。

    实现分四批 ``with_columns``，每批内部至多一次 ``.over``（避免 nested
    window），语义逐条等价于 zdk ``enrich_technical`` 里的逐行扫描：

    批 1  用「前一日是否涨停」的分组累加得到 ``_lpg``，组内行序号即
          ``zdk_lp_days``（涨停发生在 k 个交易日前）。
    批 2  把「上一次涨停当日的成交量 / 收盘价 / 涨停前 5 日均量」压成
          稀疏列后 ``forward_fill``，得到当前行的对应值。
    批 3  组内累计最小量 → 涨停之后的最缩量日。
    批 4  合成四个对外列。

    性能说明：早期版本用 ``lookback`` 个 when 分支（20×4 ≈ 80 个嵌套
    ``.over`` 表达式），在 40 万行上要 77 秒；本实现是常数个窗口算子，
    实测降到 6 秒以内，且输出逐只一致（已用基线比对验证）。
    """
    if frame.is_empty():
        return frame

    # 批 1：分组 + 距今天数
    work = frame.with_columns(
        pl.col("zdk_is_lu").fill_null(False).shift(1).over("symbol")
        .fill_null(False).cast(pl.Int32).alias("_lp_prev_lu")
    ).with_columns(
        pl.col("_lp_prev_lu").cum_sum().over("symbol").alias("_lp_grp")
    ).with_columns(
        pl.when(pl.col("_lp_grp") == 0)
        .then(pl.lit(None, dtype=pl.Int64))
        .otherwise(pl.col("_lp_grp").cum_count().over(["symbol", "_lp_grp"]))
        .alias("zdk_lp_days")
    )

    # 批 2：上一次涨停当日的量价 + 涨停前 5 日均量（稀疏后前向填充）
    base_vol = (
        pl.col("volume").shift(1).rolling_mean(5, min_samples=1).over("symbol")
    )
    work = work.with_columns([base_vol.alias("_lp_base_at_day")]).with_columns([
        pl.when(pl.col("_lp_prev_lu").cast(pl.Boolean))
        .then(pl.col("volume").shift(1).over("symbol"))
        .otherwise(None)
        .forward_fill().over("symbol").alias("zdk_lp_limit_vol"),
        pl.when(pl.col("_lp_prev_lu").cast(pl.Boolean))
        .then(pl.col("close").shift(1).over("symbol"))
        .otherwise(None)
        .forward_fill().over("symbol").alias("zdk_lp_limit_close"),
        pl.when(pl.col("_lp_prev_lu").cast(pl.Boolean))
        .then(pl.col("_lp_base_at_day").shift(1).over("symbol"))
        .otherwise(None)
        .forward_fill().over("symbol").alias("zdk_lp_base_vol"),
    ])

    # 批 3：涨停之后（组内）的最缩量日
    work = work.with_columns(
        pl.col("volume").cum_min().over(["symbol", "_lp_grp"]).alias("_lp_after_min")
    )

    # 批 4：合成对外列
    limit_vol = pl.col("zdk_lp_limit_vol")
    base = pl.col("zdk_lp_base_vol")
    limit_close = pl.col("zdk_lp_limit_close")
    after_min = pl.col("_lp_after_min")
    days = pl.col("zdk_lp_days")
    usable = days.is_not_null() & (days <= lookback)
    return work.with_columns([
        usable.fill_null(False).alias("zdk_lp_ok"),
        pl.when(usable & limit_vol.is_not_null() & (limit_vol > 0)
                & base.is_not_null() & (base > 0))
        .then(limit_vol / base)
        .otherwise(0.0).alias("zdk_lp_limit_vol_ratio"),
        pl.when(usable & limit_close.is_not_null() & (limit_close > 0))
        .then((pl.col("close") / limit_close - 1.0) * 100.0)
        .otherwise(0.0).alias("zdk_lp_pullback_pct"),
        pl.when(usable & limit_vol.is_not_null() & (limit_vol > 0)
                & after_min.is_not_null())
        .then(after_min / limit_vol)
        .otherwise(1.0).alias("zdk_lp_min_vol_ratio"),
    ]).drop([
        "_lp_prev_lu", "_lp_grp", "_lp_base_at_day",
        "zdk_lp_limit_vol", "zdk_lp_limit_close", "zdk_lp_base_vol", "_lp_after_min",
    ])


# ---------------------------------------------------------------------------
# 游资手法（zdk hotmoney.FORMATIONS 的逐条移植）
# ---------------------------------------------------------------------------

def _hot_first_board() -> pl.Expr:
    """首板套利：今日首次封板、非一字、有买入机会、近 10 日没反复炒过、位置不高。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_prev_lu").cast(pl.Boolean)
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & (pl.col("zdk_lu_10") <= 1)
        & (pl.col("zdk_roc20") < 35.0)
    )


def _hot_weak2strong() -> pl.Expr:
    """弱转强反包：昨日烂板（分歧）→ 今日直接封回（转一致）。"""
    return (
        pl.col("zdk_is_lu")
        & pl.col("zdk_prev_broken").cast(pl.Boolean)
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & ~pl.col("zdk_prev_ld").cast(pl.Boolean)
    )


def _hot_relay() -> pl.Expr:
    """连板接力：只接 1→2、2→3 且换手充分（非一字）；3 进 4 以上属中位死亡区。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & (pl.col("zdk_prev_streak") >= 1)
        & (pl.col("zdk_prev_streak") <= 2)
    )


def _hot_leader_yin() -> pl.Expr:
    """龙头首阴低吸：近期强票（近 10 日 ≥2 次涨停）首次温和缩量收阴，仍在 MA10 上。"""
    return (
        ~pl.col("zdk_is_lu")
        & ~pl.col("zdk_is_ld")
        & (pl.col("zdk_lu_10") >= 2)
        & (pl.col("zdk_pct") >= -9.0)
        & (pl.col("zdk_pct") <= -1.0)
        & pl.col("zdk_above_ma10").fill_null(False)
        & (pl.col("zdk_lb").fill_null(1.0) <= 0.9)
    )


def _hot_ma_pullback() -> pl.Expr:
    """龙回头回踩：强票缩量贴住 MA5/MA10 且未跌破 MA20。"""
    near10 = (pl.col("zdk_dist_ma10") >= -2.5) & (pl.col("zdk_dist_ma10") <= 2.0)
    near5 = (pl.col("zdk_dist_ma5") >= -2.0) & (pl.col("zdk_dist_ma5") <= 1.5)
    return (
        ~pl.col("zdk_is_lu")
        & ~pl.col("zdk_is_ld")
        & (pl.col("zdk_lu_10") >= 2)
        & (near10.fill_null(False) | near5.fill_null(False))
        & (pl.col("zdk_dist_ma20").fill_null(1.0) >= 0)
        & (pl.col("zdk_lb").fill_null(1.0) <= 1.0)
    )


def _hot_reverse_board() -> pl.Expr:
    """反包板：昨日大阴(≤-4%) → 今日封板并把昨日实体整个吞没（收盘 ≥ 昨日开盘）。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & (pl.col("zdk_prev_pct") <= -4.0)
        & (pl.col("zdk_prev_open") > 0)
        & (pl.col("close") >= pl.col("zdk_prev_open"))
    )


def _hot_second_wave() -> pl.Expr:
    """二波启动：20 日内炒过一波(≥2 次涨停) → 断板洗够 3 日 → 今日重新封板。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & (pl.col("zdk_lu_20") >= 2)
        & (pl.col("zdk_lu_3_prev") == 0)
        & (pl.col("zdk_prev_streak") == 0)
    )


def _hot_low_replace() -> pl.Expr:
    """低位补涨：距 60 日高点还有 ≥8% 空间 + 20 日内没被炒过 + 今日首次放量封板。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & (pl.col("zdk_lu_20") <= 1)
        & (pl.col("zdk_roc20") < 20.0)
        & (pl.col("zdk_high60_prev").fill_null(0.0) > 0)
        & (
            (pl.col("zdk_high60_prev") / pl.col("close") - 1.0) * 100.0 >= 8.0
        )
    )


def _hot_accel_board() -> pl.Expr:
    """加速新高板：封板同时创 60 日新高，且只接首板/一进二。"""
    return (
        pl.col("zdk_is_lu")
        & ~pl.col("zdk_one_word")
        & pl.col("zdk_openable")
        & pl.col("zdk_new_high60").fill_null(False)
        & (pl.col("zdk_prev_streak") <= 1)
    )


def _limit_pullback() -> pl.Expr:
    """涨停回马枪：近 6~9 日涨停 → 涨停日明显放量 → 回调不深 → 已缩量洗盘。

    条件取自 bigApush《涨停回马枪策略说明书》：涨停日量 ≥ 前 5 日均量 2.2 倍；
    回调 1~9 日、幅度 -15%~+5%；至少一日缩量到涨停日的 50% 以下（浮筹被洗掉）。
    """
    days = pl.col("zdk_lp_days")
    return (
        pl.col("zdk_lp_ok").fill_null(False)
        & (days >= 1) & (days <= 9)
        & (pl.col("zdk_lp_limit_vol_ratio").fill_null(0.0) >= 2.2)
        & (pl.col("zdk_lp_pullback_pct").fill_null(0.0) >= -15.0)
        & (pl.col("zdk_lp_pullback_pct").fill_null(0.0) <= 5.0)
        & (pl.col("zdk_lp_min_vol_ratio").fill_null(1.0) <= 0.5)
    )


def _limit_sideways() -> pl.Expr:
    """涨停横盘：近 10 日涨停 → 放量 → 之后窄幅横盘不破位（-5%~+8%）。

    zdk 注释：原策略另要求 KDJ 金叉 + 放量突破，那部分交给 tech_bonus 加分，
    不做硬性淘汰，避免条件过密导致选不出票。
    """
    days = pl.col("zdk_lp_days")
    return (
        pl.col("zdk_lp_ok").fill_null(False)
        & (days >= 1) & (days <= 10)
        & (pl.col("zdk_lp_limit_vol_ratio").fill_null(0.0) >= 1.8)
        & (pl.col("zdk_lp_pullback_pct").fill_null(0.0) >= -5.0)
        & (pl.col("zdk_lp_pullback_pct").fill_null(0.0) <= 8.0)
    )


# ---------------------------------------------------------------------------
# 「要信早信」/「心法框架」专用上下文（市场宽度、题材共振、板块多头）
# ---------------------------------------------------------------------------
#
# 这两条策略在 zdk 里依赖三类 TSP enriched 帧没有的数据：
#   1. 涨停池的同花顺 ``reason_type``（题材标签）与首封时间 / 开板次数；
#   2. 行业板块的等权指数（需要自己按成分聚合）；
#   3. 全市场情绪阶段（需要全市场涨停/炸板/跌停家数）。
# 第 3 类可以完全由 enriched 帧自算；第 2 类由 ``_zdk_boards`` 的字面量快照
# 补行业归属后自算；第 1 类用「同概念涨停家数」代理（``_zdk_concepts``），
# 首封时间与开板次数在日K层面不可得，直接降级（见两个策略的 RULES）。

#: zdk ``hotmoney.STAGE_ORDER``：情绪阶段由弱到强。
STAGE_ORDER: tuple[str, ...] = ("decline", "ice", "trial", "main_up", "high_chaos")

#: zdk ``hotmoney.STAGE_TEXT``：阶段中文名。
STAGE_TEXT: dict[str, str] = {
    "decline": "主跌期",
    "ice": "冰点期",
    "trial": "低位试错",
    "main_up": "主升期",
    "high_chaos": "高位震荡",
}

#: zdk ``hotmoney.TH`` 阈值。百分比类是「占全市场只数的百分数」。
STAGE_THRESHOLDS: dict[str, float] = {
    "decline_ld_pct": 0.25,     # 跌停占比 >= 0.25% → 主跌
    "decline_broken": 0.45,     # 炸板率 >= 45% 且涨停占比 < 0.6% → 主跌
    "ice_lu_pct": 0.6,          # 涨停占比 < 0.6% → 冰点
    "main_up_lu_pct": 2.0,      # 主升：涨停占比 >= 2%
    "main_up_streak": 4,        # 且最高连板 >= 4
    "main_up_broken": 0.28,     # 且炸板率 < 28%
    "chaos_lu_pct": 1.2,        # 高位震荡：涨停占比 >= 1.2%
    "chaos_broken": 0.32,       # 且炸板率 >= 32%
}

#: 「要信早信」默认阈值（对齐 zdk ``yaoxin.P``）。
YAOXIN_DEFAULTS: dict[str, float] = {
    "min_streak": 4.0,          # 昨日最高连板家数下限
    "break_ratio": 1.0,         # 需要断板的高标比例（1.0 = 全部断板）
    "node_gain_max": 15.0,      # 节点以来涨幅上限（%）
    "abs20_max": 40.0,          # 近 20 日累计涨幅上限（%）
    "min_amount_yi": 3.0,       # 容量闸：当日成交额下限（亿元）
    "trend10_min": 15.0,        # 趋势确认：前 10 交易日涨幅下限（%）
    "trend10_max": 50.0,        # 趋势确认：前 10 交易日涨幅上限（%）
    "theme_min": 3,             # 同题材涨停达到几家才开始给题材加分
}

#: 「心法框架」默认阈值（对齐 zdk framework 模块常量）。
FRAMEWORK_DEFAULTS: dict[str, float] = {
    "mkt_turnover_yi": 23000.0,  # 两市成交额分档线（亿元）
    "amt3_min_yi": 4.5,          # 3 日均额下限（亿元）
    "amt3_floor_yi": 1.0,        # 缩量时下限的硬地板（亿元）
    "amt3_max_lo_yi": 50.0,      # 低档上限（亿元）
    "amt3_max_hi_yi": 70.0,      # 高档上限（亿元）
    "mv_max_yi": 1000.0,         # 总市值上限（亿元）
    "bull_days": 3.0,            # 板块需连续多头成立的交易日数
    "index_members": 10.0,       # 板块等权指数取市值前几只成分
    "board_min_members": 5.0,    # 板块最少成分数
}

#: 要信早信放行的情绪阶段（zdk ``P["gate_stages"]``）。
YAOXIN_GATE_STAGES: tuple[str, ...] = ("trial", "high_chaos")


def _stage_expr() -> pl.Expr:
    """全市场情绪阶段判定的 when/then 级联（顺序即优先级，对齐 zdk）。"""
    lu_pct = pl.col("_yx_lu_pct")
    broken = pl.col("_yx_broken_rate")
    ld_pct = pl.col("_yx_ld_pct")
    max_streak = pl.col("_yx_max_streak")
    th = STAGE_THRESHOLDS
    return (
        pl.when(
            (ld_pct >= th["decline_ld_pct"])
            | ((broken >= th["decline_broken"]) & (lu_pct < th["ice_lu_pct"]))
        ).then(pl.lit("decline"))
        .when(
            (lu_pct >= th["main_up_lu_pct"])
            & (max_streak >= th["main_up_streak"])
            & (broken < th["main_up_broken"])
        ).then(pl.lit("main_up"))
        .when(
            (lu_pct >= th["chaos_lu_pct"]) & (broken >= th["chaos_broken"])
        ).then(pl.lit("high_chaos"))
        .when(lu_pct < th["ice_lu_pct"]).then(pl.lit("ice"))
        .otherwise(pl.lit("trial"))
    )


def _market_breadth(frame: pl.DataFrame) -> pl.DataFrame:
    """全市场（按交易日）涨停/跌停/炸板家数、最高连板与情绪阶段。

    只依赖 ``add_derived`` 已落地的 ``zdk_prev_lu`` / ``zdk_prev_ld`` /
    ``zdk_prev_broken`` / ``zdk_prev_streak``：与 zdk 一致，闸门看的是
    **昨日**的梯队记录（``yrec``），所以这里全部取前一日口径。

    与 zdk 的差异：zdk 用固定常数 ``YX_UNIVERSE_N=4600`` 做分母，这里用帧内
    当日实际标的数（实测 5570 左右）。分母变大后同样的家数算出的占比更小，
    即闸门略微更紧 —— 这是有意的：用真实全市场只数比写死常数更可解释。
    """
    frame = frame.with_columns(
        pl.col("zdk_prev_lu").cast(pl.Int32).sum().over("date").alias("_yx_lu_n"),
        pl.col("zdk_prev_ld").cast(pl.Int32).sum().over("date").alias("_yx_ld_n"),
        pl.col("zdk_prev_broken").cast(pl.Int32).sum().over("date").alias("_yx_zb_n"),
        pl.col("zdk_prev_streak").cast(pl.Int32).max().over("date")
        .alias("_yx_max_streak"),
        pl.col("symbol").count().over("date").alias("_yx_universe"),
    )
    frame = frame.with_columns(
        (pl.col("_yx_lu_n") / pl.col("_yx_universe") * 100.0).alias("_yx_lu_pct"),
        (pl.col("_yx_ld_n") / pl.col("_yx_universe") * 100.0).alias("_yx_ld_pct"),
        (
            pl.col("_yx_zb_n")
            / (pl.col("_yx_lu_n") + pl.col("_yx_zb_n")).clip(1, None)
        ).alias("_yx_broken_rate"),
    )
    frame = frame.with_columns(
        _stage_expr().alias("zdk_stage"),
        pl.col("_yx_lu_n").alias("zdk_lu_count"),
        pl.col("_yx_zb_n").alias("zdk_broken_count"),
        pl.col("_yx_max_streak").alias("zdk_max_streak"),
    )
    return frame.drop(["_yx_lu_n", "_yx_ld_n", "_yx_zb_n", "_yx_universe"])


def _yaoxin_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """要信早信的断板触发、节点涨幅、趋势确认、题材共振与容量闸。

    zdk 的判定基准是「今日的涨停池 + 昨日及之前的日K」，因此这些列的口径是：

    * ``zdk_yx_trigger``：昨日最高连板 >= ``min_streak`` 且昨日高标断板比例
      达到 ``break_ratio``（1.0 = 全部断板）。
    * ``zdk_node_price``：断板高标各自连板段起点的收盘价取最小（zdk 取的是
      「起点日期最小」，这里用价格最小近似 —— 两者在单调行情下等价，在起点
      价格顺序颠倒时会略偏严格）。
    * ``zdk_node_gain`` / ``zdk_gain20`` / ``zdk_trend10``：全部用**昨日**收盘
      价作为「当前价」，与 zdk ``_judge_common`` 一致。
    """
    frame = frame.with_columns(
        pl.col("zdk_is_lu").fill_null(False).alias("_yx_lu"),
        # zdk_prev_lu 由 add_derived 落成 Int32（shift+fill_null(0)），
        # 这里必须显式转 Boolean，否则 & ~ 会退化成 i32 位运算。
        pl.col("zdk_prev_lu").cast(pl.Boolean).fill_null(False).alias("_yx_prev_lu"),
    )
    frame = frame.with_columns(
        (pl.col("_yx_lu") & ~pl.col("_yx_prev_lu")).alias("_yx_streak_start")
    )
    # 连板段起点日期；断板之后 forward_fill 仍指向最近一段的起点。
    frame = frame.with_columns(
        pl.when(pl.col("_yx_streak_start"))
        .then(pl.col("date"))
        .otherwise(pl.lit(None, dtype=pl.Date))
        .forward_fill()
        .over("symbol")
        .alias("_yx_streak_start_date")
    )
    # 昨日高标 = 昨日连板数与昨日全市场最高连板数相同的那批。
    frame = frame.with_columns(
        (
            pl.col("zdk_prev_streak").fill_null(0)
            == pl.col("_yx_max_streak").fill_null(0)
        ).alias("_yx_top")
    )
    frame = frame.with_columns(
        (pl.col("_yx_top") & ~pl.col("_yx_lu")).alias("_yx_broken_top")
    )
    frame = frame.with_columns(
        pl.col("_yx_top").cast(pl.Int32).sum().over("date").alias("_yx_tops_n"),
        pl.col("_yx_broken_top").cast(pl.Int32).sum().over("date")
        .alias("_yx_broken_n"),
        # 节点日期 = 各断板高标连板段起点日期的最小值，对齐 zdk
        # `node = min(nodes)`（`_node_start` 回溯的是**日期**，不是价格）。
        pl.when(pl.col("_yx_broken_top"))
        .then(pl.col("_yx_streak_start_date"))
        .otherwise(pl.lit(None, dtype=pl.Date))
        .min()
        .over("date")
        .alias("zdk_node_date"),
    )
    # 节点价 = **候选自己**在节点日的收盘价（zdk `_judge_common`：
    # `node_gain = (hc[-1]/hc[jn]-1)*100`，jn 是节点日在该票自身日K里的下标）。
    # 必须按 (symbol, node_date) 回查，不能拿断板高标的起点价冒充。
    node_close = frame.select(
        pl.col("symbol"),
        pl.col("date").alias("zdk_node_date"),
        pl.col("close").alias("zdk_node_price"),
    )
    frame = frame.join(node_close, on=["symbol", "zdk_node_date"], how="left")
    frame = frame.with_columns(
        (
            (pl.col("_yx_max_streak").fill_null(0) >= YAOXIN_DEFAULTS["min_streak"])
            & (
                pl.col("_yx_broken_n").cast(pl.Float64)
                >= (
                    pl.col("_yx_tops_n").cast(pl.Float64)
                    * YAOXIN_DEFAULTS["break_ratio"]
                ).round(0).clip(1.0, None)
            )
        ).alias("zdk_yx_trigger"),
        pl.col("zdk_stage")
        .is_in(list(YAOXIN_GATE_STAGES))
        .fill_null(False)
        .alias("zdk_yx_stage_ok"),
        pl.col("_yx_broken_n").alias("zdk_broken_tops"),
    )
    frame = frame.with_columns(
        pl.col("close").shift(1).over("symbol").alias("_yx_prev_close")
    )
    frame = frame.with_columns(
        (
            (pl.col("_yx_prev_close") / pl.col("close").shift(11).over("symbol") - 1.0)
            * 100.0
        ).alias("zdk_trend10"),
        (
            (pl.col("_yx_prev_close") / pl.col("close").shift(21).over("symbol") - 1.0)
            * 100.0
        ).alias("zdk_gain20"),
        (
            (pl.col("_yx_prev_close") / pl.col("zdk_node_price") - 1.0) * 100.0
        ).alias("zdk_node_gain"),
    )
    return frame.drop(
        [
            "_yx_lu", "_yx_prev_lu", "_yx_streak_start", "_yx_streak_start_date",
            "_yx_top", "_yx_broken_top", "_yx_tops_n", "_yx_broken_n",
            "_yx_prev_close", "_yx_lu_pct", "_yx_ld_pct", "_yx_broken_rate",
            "_yx_max_streak",
        ]
    )


def _theme_hits(frame: pl.DataFrame, concept_map: dict[str, tuple[str, ...]]) -> pl.DataFrame:
    """同概念涨停家数（zdk ``reason_type`` 题材家数的代理）。

    zdk 用同花顺涨停池的 ``reason_type``（一个票可能挂多个题材）统计「同一
    题材当日有几家涨停」，取候选所属题材里的最大值。TSP 没有涨停池题材，
    但 ``_zdk_concepts`` 提供了同花顺概念成分，于是按「同概念的当日涨停家数」
    复刻同一语义：候选挂着的主升题材里今天有几家涨停。

    只对首板行（``zdk_streak <= 1``）计算，其余行留 0 —— 该列只参与候选排序。
    """
    pairs: list[tuple[str, str]] = []
    for code, names in (concept_map or {}).items():
        for name in names:
            pairs.append((code, name))
    if not pairs:
        return frame.with_columns(pl.lit(0.0).alias("zdk_theme_hits"))
    table = pl.DataFrame(
        {"code6": [row[0] for row in pairs], "concept": [row[1] for row in pairs]},
        schema={"code6": pl.Utf8, "concept": pl.Utf8},
    )
    base = frame.select(
        ["symbol", "date", "zdk_is_lu", "zdk_streak"]
    ).with_columns(pl.col("symbol").str.slice(0, 6).alias("code6"))
    # 当日全部涨停股的 (日期, 概念) 家数。
    lu_pairs = (
        base.filter(pl.col("zdk_is_lu").fill_null(False))
        .select(["symbol", "date", "code6"])
        .join(table, on="code6", how="inner")
    )
    counts = lu_pairs.group_by(["date", "concept"]).agg(
        pl.col("symbol").n_unique().alias("n")
    )
    candidates = (
        base.filter(pl.col("zdk_streak").fill_null(0) <= 1)
        .select(["symbol", "date", "code6"])
        .join(table, on="code6", how="inner")
        .join(counts, on=["date", "concept"], how="left")
    )
    hits = candidates.group_by(["symbol", "date"]).agg(
        pl.col("n").max().alias("zdk_theme_hits")
    )
    return (
        frame.join(hits, on=["symbol", "date"], how="left")
        .with_columns(pl.col("zdk_theme_hits").fill_null(0.0))
    )


def add_yaoxin_context(
    frame: pl.DataFrame,
    concept_map: dict[str, tuple[str, ...]] | None = None,
) -> pl.DataFrame:
    """「要信早信」专用上下文列（在 ``add_derived`` 之后、粗筛之前调用）。

    末尾把 zdk ``yaoxin.run`` 的第 ⑤ 段分值拆成三个可加权列，供
    ``score_mode="raw"`` 直接相加复刻原公式（见 ``_RAW_WEIGHTS_MAP``）：

    * ``zdk_base_score`` = ``50.0``（zdk 的保底分）
    * ``zdk_theme_score`` = ``min(tn, 10) * 3``（``tn >= theme_min`` 时才有，
      否则 0）
    * ``zdk_node_lo`` = ``max(0, 15 - node_gain) * 0.8``（越早位置分越高）

    zdk 里另外两个加分项（首封早于 10:30 的 +30、炸板回封的 +20）在日K层面
    完全不可得，**直接归零**，因此本策略的分值上限是 50 + 30 + 12 = 92 而不是
    zdk 的 142 —— 见策略文件 RULES 的「降级」一节。
    """
    frame = _market_breadth(frame)
    frame = _yaoxin_columns(frame)
    frame = _theme_hits(frame, concept_map or {})
    theme_min = float(YAOXIN_DEFAULTS["theme_min"])
    node_gain_max = float(YAOXIN_DEFAULTS["node_gain_max"])
    return frame.with_columns(
        pl.lit(50.0).alias("zdk_base_score"),
        pl.when(pl.col("zdk_theme_hits").fill_null(0.0) >= theme_min)
        .then(
            pl.min_horizontal(pl.col("zdk_theme_hits").fill_null(0.0), pl.lit(10.0))
            * 3.0
        )
        .otherwise(0.0)
        .alias("zdk_theme_score"),
        (node_gain_max - pl.col("zdk_node_gain").fill_null(node_gain_max))
        .clip(lower_bound=0.0)
        .alias("zdk_node_lo"),
    )


def _board_bull_days(
    frame: pl.DataFrame, board_map: dict[str, str]
) -> pl.DataFrame:
    """行业板块等权指数多头成立的天数（zdk ``framework.board_bull`` 的移植）。

    口径：每个行业取总市值前 ``index_members``(10) 只成分做等权指数，
    ``MA5`` / ``MACD(12,26,9)`` / ``EXPMA12`` / ``EXPMA50`` 全部自算；
    连续 3 日同时满足「收盘 > MA5 ｜ DIF > DEA ｜ EMA12 > EMA50」即多头。

    与 zdk 的差异：zdk 的 ``board_bull`` 还会把「末日只有部分成分更新」的
    行截断掉（避免指数被半天数据带偏），TSP 的 enriched 帧按交易日对齐、
    不存在该问题，故未复刻。
    """
    if not board_map:
        return frame.with_columns(
            pl.lit(FRAMEWORK_DEFAULTS["bull_days"], dtype=pl.Int32)
            .alias("zdk_board_bull_days")
        )
    members = frame.select(["symbol", "date", "close", "total_shares"]).with_columns(
        pl.col("symbol").str.slice(0, 6).alias("_code6")
    )
    members = members.with_columns(
        pl.col("_code6").replace_strict(
            board_map, default=None, return_dtype=pl.Utf8
        ).alias("zdk_board"),
        (pl.col("close") * pl.col("total_shares") / 1e8).alias("_tmv"),
    ).filter(
        pl.col("zdk_board").is_not_null() & pl.col("_tmv").is_not_null()
    )
    if members.is_empty():
        return frame.with_columns(
            pl.lit(FRAMEWORK_DEFAULTS["bull_days"], dtype=pl.Int32)
            .alias("zdk_board_bull_days")
        )
    members = members.with_columns(
        pl.col("_tmv")
        .rank(method="ordinal", descending=True)
        .over(["zdk_board", "date"])
        .alias("_rank")
    ).filter(pl.col("_rank") <= FRAMEWORK_DEFAULTS["index_members"])

    index = (
        members.group_by(["zdk_board", "date"])
        .agg(
            pl.col("close").mean().alias("_idx"),
            pl.col("symbol").count().alias("_n"),
        )
        .filter(pl.col("_n") >= FRAMEWORK_DEFAULTS["board_min_members"])
        .sort(["zdk_board", "date"])
    )
    if index.is_empty():
        return frame.with_columns(
            pl.lit(FRAMEWORK_DEFAULTS["bull_days"], dtype=pl.Int32)
            .alias("zdk_board_bull_days")
        )
    index = index.with_columns(
        pl.col("_idx").rolling_mean(5, min_samples=2).over("zdk_board").alias("_ma5"),
        pl.col("_idx").ewm_mean(span=12, adjust=False).over("zdk_board").alias("_e12"),
        pl.col("_idx").ewm_mean(span=26, adjust=False).over("zdk_board").alias("_e26"),
        pl.col("_idx").ewm_mean(span=50, adjust=False).over("zdk_board").alias("_e50"),
    )
    index = index.with_columns((pl.col("_e12") - pl.col("_e26")).alias("_dif"))
    index = index.with_columns(
        pl.col("_dif").ewm_mean(span=9, adjust=False).over("zdk_board").alias("_dea")
    )
    index = index.with_columns(
        (
            (pl.col("_idx") > pl.col("_ma5"))
            & (pl.col("_dif") > pl.col("_dea"))
            & (pl.col("_e12") > pl.col("_e50"))
        )
        .fill_null(False)
        .alias("_bull")
    )
    index = index.with_columns(
        (~pl.col("_bull")).cast(pl.Int32).cum_sum().over("zdk_board").alias("_grp")
    )
    index = index.with_columns(
        pl.col("_bull")
        .cast(pl.Int32)
        .cum_sum()
        .over(["zdk_board", "_grp"])
        .alias("zdk_board_bull_days")
    )
    picked = index.select(["zdk_board", "date", "zdk_board_bull_days"])
    frame = frame.with_columns(
        pl.col("symbol")
        .str.slice(0, 6)
        .replace_strict(board_map, default=None, return_dtype=pl.Utf8)
        .alias("zdk_board")
    )
    return frame.join(picked, on=["zdk_board", "date"], how="left").with_columns(
        pl.col("zdk_board_bull_days").fill_null(0).cast(pl.Int32)
    )


def add_framework_context(
    frame: pl.DataFrame,
    board_map: dict[str, str] | None = None,
) -> pl.DataFrame:
    """「心法框架」专用上下文列（在 ``add_derived`` 之后、粗筛之前调用）。

    负责生成框架策略独有的一批列：

    * ``zdk_board`` / ``zdk_board_bull_days``：行业归属与板块多头天数；
    * ``zdk_totalcap``：总市值（亿元，用于 ``MV_MAX`` 过滤）；
    * ``zdk_cum5`` / ``zdk_pct10``：近 5 日累计涨幅、近十日涨幅（剔除条件）；
    * ``zdk_amt3_yi`` / ``zdk_amt3_ok``：3 日均额（亿元）与动态带内判定。
    """
    frame = frame.with_columns(
        (pl.col("close") * pl.col("total_shares") / 1e8).alias("zdk_totalcap"),
        (
            (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1.0) * 100.0
        ).alias("zdk_cum5"),
        (
            (
                pl.col("close").shift(1).over("symbol")
                / pl.col("close").shift(11).over("symbol")
                - 1.0
            )
            * 100.0
        ).alias("zdk_pct10"),
        (
            (pl.col("volume") * 100.0 * pl.col("close"))
            .rolling_mean(3, min_samples=1)
            .over("symbol")
            / 1e8
        ).alias("zdk_amt3_yi"),
        pl.col("amount").sum().over("date").alias("_fw_mkt_amt"),
    )
    frame = frame.with_columns(
        (pl.col("_fw_mkt_amt") / 1e8).alias("zdk_mkt_amt_yi")
    )
    # 缩量时成交额下限按两市成交额等比缩放，并有 1 亿硬地板（zdk amt3_mode="market"）。
    frame = frame.with_columns(
        pl.max_horizontal(
            pl.lit(FRAMEWORK_DEFAULTS["amt3_floor_yi"]),
            pl.lit(FRAMEWORK_DEFAULTS["amt3_min_yi"])
            * (
                pl.col("zdk_mkt_amt_yi") / FRAMEWORK_DEFAULTS["mkt_turnover_yi"]
            ).clip(upper_bound=1.0),
        ).alias("zdk_amt3_floor"),
        pl.when(
            pl.col("zdk_mkt_amt_yi") < FRAMEWORK_DEFAULTS["mkt_turnover_yi"]
        )
        .then(pl.lit(FRAMEWORK_DEFAULTS["amt3_max_lo_yi"]))
        .otherwise(pl.lit(FRAMEWORK_DEFAULTS["amt3_max_hi_yi"]))
        .alias("zdk_amt3_cap"),
    )
    frame = frame.with_columns(
        (
            (pl.col("zdk_amt3_yi") >= pl.col("zdk_amt3_floor"))
            & (pl.col("zdk_amt3_yi") <= pl.col("zdk_amt3_cap"))
        )
        .fill_null(False)
        .alias("zdk_amt3_ok"),
        pl.col("close").shift(11).over("symbol").is_not_null().alias("zdk_bars_ok"),
    )
    frame = frame.drop("_fw_mkt_amt")
    return _board_bull_days(frame, board_map or {})


#: 技术面硬性确认条件。键与 zdk screener.TECH_RULES 同名，语义逐条对齐。
TECH_RULES: dict[str, pl.Expr] = {
    "above_ma20": pl.col("zdk_above_ma20").fill_null(False),
    "above_ma60": pl.col("zdk_above_ma60").fill_null(False),
    "bull_stack": pl.col("zdk_bull_stack").fill_null(False),
    "new_high20": pl.col("zdk_new_high20").fill_null(False),
    "new_high60": pl.col("zdk_new_high60").fill_null(False),
    "macd_gold": pl.col("zdk_macd_gold").fill_null(False),
    "pullback_ma20": pl.col("zdk_pullback_ma20").fill_null(False),
    "rsi_oversold": pl.col("rsi_14").fill_null(100.0) < 38.0,
    "rsi_mid": (
        (pl.col("rsi_14").fill_null(0.0) >= 40.0)
        & (pl.col("rsi_14").fill_null(0.0) <= 68.0)
    ),
    "vol_ratio5_hi": pl.col("zdk_lb").fill_null(0.0) >= 1.5,
    "limit_pullback": _limit_pullback().fill_null(False),
    "limit_sideways": _limit_sideways().fill_null(False),
    "hot_first_board": _hot_first_board().fill_null(False),
    "hot_weak2strong": _hot_weak2strong().fill_null(False),
    "hot_relay": _hot_relay().fill_null(False),
    "hot_leader_yin": _hot_leader_yin().fill_null(False),
    "hot_ma_pullback": _hot_ma_pullback().fill_null(False),
    "hot_reverse_board": _hot_reverse_board().fill_null(False),
    "hot_second_wave": _hot_second_wave().fill_null(False),
    "hot_low_replace": _hot_low_replace().fill_null(False),
    "hot_accel_board": _hot_accel_board().fill_null(False),
    # --- 以下 20 条只服务「要信早信 / 心法框架」两条特殊策略 -------------
    # 「要信早信」六道闸门（zdk yaoxin.run 的 ①②③④⑤⑥）。
    "yx_trigger": pl.col("zdk_yx_trigger").fill_null(False),
    "yx_stage_ok": pl.col("zdk_yx_stage_ok").fill_null(False),
    "yx_first_board": (
        pl.col("zdk_is_lu").fill_null(False)
        & (pl.col("zdk_streak").fill_null(0) <= 1)
    ),
    "yx_node_low": pl.col("zdk_node_gain").fill_null(0.0)
    <= YAOXIN_DEFAULTS["node_gain_max"],
    "yx_gain20_ok": pl.col("zdk_gain20").fill_null(0.0)
    <= YAOXIN_DEFAULTS["abs20_max"],
    "yx_trend_ok": (
        (pl.col("zdk_trend10").fill_null(0.0) >= YAOXIN_DEFAULTS["trend10_min"])
        & (pl.col("zdk_trend10").fill_null(0.0) <= YAOXIN_DEFAULTS["trend10_max"])
    ),
    "yx_amount_ok": pl.col("amount").fill_null(0.0)
    >= YAOXIN_DEFAULTS["min_amount_yi"] * 1e8,
    "yx_theme_ok": pl.col("zdk_theme_hits").fill_null(0.0)
    >= YAOXIN_DEFAULTS["theme_min"],
    # 「心法框架」九条限制（zdk framework.run 的 ③④⑤⑥⑦⑨ + 今日涨停剔除）。
    "fw_board_bull": pl.col("zdk_board_bull_days").fill_null(0)
    >= FRAMEWORK_DEFAULTS["bull_days"],
    "fw_bars_ok": pl.col("zdk_bars_ok").fill_null(False),
    "fw_mv_ok": (
        (pl.col("zdk_totalcap").fill_null(0.0) > 0.0)
        & (pl.col("zdk_totalcap").fill_null(0.0) < FRAMEWORK_DEFAULTS["mv_max_yi"])
    ),
    "fw_pct10_ok": pl.col("zdk_pct10").fill_null(0.0) <= 95.0,
    "fw_lu5_ok": pl.col("zdk_lu_5").fill_null(0) < 4,
    "fw_ow20_ok": ~pl.col("zdk_ow_20").fill_null(False),
    "fw_amt3_ok": pl.col("zdk_amt3_ok").fill_null(False),
    "fw_cum5_ok": pl.col("zdk_cum5").fill_null(0.0) > 0.0,
    "fw_kdj_ok": (
        (pl.col("kdj_k").fill_null(0.0) > pl.col("kdj_d").fill_null(0.0))
        & (pl.col("kdj_j").fill_null(0.0) <= 110.0)
    ),
    "fw_not_limit": ~pl.col("zdk_is_lu").fill_null(False),
}

#: 技术加分项里两条特殊策略专属的键（与 BONUS_RULES 合并查找）。
EXTRA_BONUS_RULES: dict[str, pl.Expr] = {
    "yx_early_seal": pl.lit(False),  # 占位：首封时间在日K层面不可得（见 RULES）
    "yx_node_early": pl.col("zdk_node_gain").fill_null(0.0) < 5.0,
    "fw_kdj_gold": pl.col("kdj_k").fill_null(0.0) > pl.col("kdj_d").fill_null(0.0),
    "fw_cum5_pos": pl.col("zdk_cum5").fill_null(0.0) > 0.0,
}

#: 技术加分项（只影响排序，不做硬淘汰）。
BONUS_RULES: dict[str, pl.Expr] = {
    "bull_stack": pl.col("zdk_bull_stack").fill_null(False),
    "macd_gold": pl.col("zdk_macd_gold").fill_null(False),
    "above_ma60": pl.col("zdk_above_ma60").fill_null(False),
    "new_high20": pl.col("zdk_new_high20").fill_null(False),
    "new_high60": pl.col("zdk_new_high60").fill_null(False),
    "vol_ratio5_hi": pl.col("zdk_lb").fill_null(0.0) >= 1.5,
    "rsi_mid": (
        (pl.col("rsi_14").fill_null(0.0) >= 40.0)
        & (pl.col("rsi_14").fill_null(0.0) <= 68.0)
    ),
}


# ---------------------------------------------------------------------------
# 第 1 层：粗筛（zdk apply_prefilter）
# ---------------------------------------------------------------------------

#: zdk prefilter 键 → (TSP 列, 运算符)。值是 zdk 侧单位，按 _PREFILTER_SCALE 换算。
_PREFILTER_MAP: dict[str, tuple[str, str]] = {
    "pct_min": ("zdk_pct", ">="),
    "pct_max": ("zdk_pct", "<="),
    "d5_min": ("momentum_5d", ">="),
    "d5_max": ("momentum_5d", "<="),
    "d10_min": ("momentum_10d", ">="),
    "d10_max": ("momentum_10d", "<="),
    "d20_min": ("momentum_20d", ">="),
    "d20_max": ("momentum_20d", "<="),
    "d60_min": ("momentum_60d", ">="),
    "d60_max": ("momentum_60d", "<="),
    "lb_min": ("zdk_lb", ">="),
    "lb_max": ("zdk_lb", "<="),
    "hsl_min": ("turnover_rate", ">="),
    "hsl_max": ("turnover_rate", "<="),
    "turnover_min": ("amount", ">="),
    "floatcap_min": ("zdk_floatcap", ">="),
    "floatcap_max": ("zdk_floatcap", "<="),
    "dist_min": ("zdk_dist_to_limit", ">="),
    "dist_max": ("zdk_dist_to_limit", "<="),
}

#: 需要单位换算的 prefilter 键（zdk 侧单位 → TSP 侧单位的倍数）。
_PREFILTER_SCALE: dict[str, float] = {
    "d5_min": 0.01, "d5_max": 0.01,
    "d10_min": 0.01, "d10_max": 0.01,
    "d20_min": 0.01, "d20_max": 0.01,
    "d60_min": 0.01, "d60_max": 0.01,
    "turnover_min": 1e4,   # 万元 → 元
}


def apply_prefilter(df: pl.DataFrame, prefilter: dict[str, Any]) -> pl.DataFrame:
    """第 1 层粗筛：策略专属阈值。

    价格/成交额下限、排除 ST 与新股、exclude_limit 等通用可交易性条件与
    TSP 的 ``META.basic_filter`` 完全重合，交给引擎那层做，这里只补
    basic_filter 表达不了的区间/派生因子条件。
    """
    exprs: list[pl.Expr] = []
    for key, value in (prefilter or {}).items():
        if key in UNSUPPORTED_PREFILTER or value is None:
            continue
        target = _PREFILTER_MAP.get(key)
        if target is None:
            continue
        column, op = target
        threshold = float(value) * _PREFILTER_SCALE.get(key, 1.0)
        ref = pl.col(column)
        exprs.append((ref >= threshold) if op == ">=" else (ref <= threshold))
    if not exprs:
        return df
    combined = exprs[0]
    for expression in exprs[1:]:
        combined = combined & expression
    return df.filter(combined.fill_null(False))


# ---------------------------------------------------------------------------
# 第 2 层：横截面百分位排名打分（zdk score_cross_section）
# ---------------------------------------------------------------------------

#: zdk 权重键 → TSP 列。
_WEIGHTS_MAP: dict[str, str] = {
    "pct": "zdk_pct",
    "d5": "momentum_5d",
    "d10": "momentum_10d",
    "d20": "momentum_20d",
    "d60": "momentum_60d",
    "lb": "zdk_lb",
    "hsl": "turnover_rate",
    "turnover": "amount",
    "zf": "zdk_zf",
    "dist_to_limit": "zdk_dist_to_limit",
    # 以下键只有「要信早信 / 心法框架」两条策略用得到：列由 add_yaoxin_context /
    # add_framework_context 在 \`run_spec\` 的 enrich 钩子里补出来。其余 22 条
    # 策略不使用这些键，加在这里不影响它们。
    "theme": "zdk_theme_hits",
    "node_gain": "zdk_node_gain",
    "trend10": "zdk_trend10",
    "gain20": "zdk_gain20",
    "board_days": "zdk_board_bull_days",
}

#: raw 打分模式下的权重键 → TSP 列。这两条特殊策略的 zdk 分值本身就是
#: 「原始值线性加权」而不是百分位加权，用 ``score_mode="raw"`` 直接复刻：
#: ``zdk_score = Σ(列值 × 权重)``，权重就是 zdk 公式里的系数。
_RAW_WEIGHTS_MAP: dict[str, str] = {
    "base": "zdk_base_score",
    "theme_score": "zdk_theme_score",
    "node_lo": "zdk_node_lo",
    "cum5": "zdk_cum5",
    "amt3": "zdk_amt3_yi",
}


def _percentile(column: str, negative: bool) -> pl.Expr:
    """截面百分位排名（对齐 zdk 的 ``series.rank(pct=True)`` 语义）。

    权重为正 = 越大越好（排名升序 → 大值拿高百分位）；键名带 ``_neg`` =
    越小越好（取 ``1 - 百分位``）。zdk 曾在这里写成 ``ascending=neg``，导致
    所有因子整体反向计分（「超跌反转」实际在选动量最强的票、「低波稳健」
    实际在选高波动票），本实现取已修正的口径。

    zdk 另有「有效样本 < 3 个就跳过该因子」的保护，这里用 ``count >= 3``
    门控：不满足时该因子退化为中性 0.5（等价于不计入区分度）。
    """
    rank = pl.col(column).rank(method="average").over("date").cast(pl.Float64)
    count = pl.col(column).count().over("date").cast(pl.Float64)
    percentile = (rank / count).fill_null(0.5)
    if negative:
        percentile = 1.0 - percentile
    return (
        pl.when(count.fill_null(0.0) >= 3.0)
        .then(percentile)
        .otherwise(0.5)
    )


def apply_scoring(
    df: pl.DataFrame,
    weights: dict[str, float],
    mode: str = "percentile",
) -> tuple[pl.DataFrame, list[str]]:
    """第 2 层：横截面百分位排名加权 → ``zdk_score``（0~100）。

    返回 (带 zdk_score 的帧, 因 TSP 缺数据而被剔除的 zdk 权重键列表)。

    ``mode="raw"`` 时不做百分位排名，直接把列值按权重线性相加（不归一化），
    用于复刻「要信早信 / 心法框架」在 zdk 里的原始分值口径 —— 那两条策略的
    分值本来就是可加的量纲（yaoxin 是 50/30/20 加法，framework 是
    ``cum5*0.6 + amt3亿*0.4``），再套百分位反而会改变语义。
    """
    if mode == "raw":
        raw_terms: list[pl.Expr] = []
        for factor, weight in (weights or {}).items():
            column = _RAW_WEIGHTS_MAP.get(factor)
            if column is None or not weight:
                continue
            raw_terms.append(
                pl.col(column).fill_null(0.0).cast(pl.Float64) * float(weight)
            )
        if not raw_terms:
            return df.with_columns(pl.lit(0.0).alias("zdk_score")), []
        score = raw_terms[0]
        for term in raw_terms[1:]:
            score = score + term
        return (
            df.with_columns(score.round(2).alias("zdk_score")),
            [],
        )
    terms: list[pl.Expr] = []
    total_weight = 0.0
    dropped: list[str] = []
    for factor, weight in (weights or {}).items():
        if not weight:
            continue
        negative = factor.endswith("_neg")
        key = factor[:-4] if negative else factor
        if key in UNSUPPORTED_WEIGHTS:
            dropped.append(factor)
            continue
        column = _WEIGHTS_MAP.get(key)
        if column is None:
            dropped.append(factor)
            continue
        terms.append(_percentile(column, negative) * float(weight))
        total_weight += float(weight)

    if not terms or total_weight <= 0 or df.is_empty():
        return df.with_columns(pl.lit(0.0).alias("zdk_score")), dropped

    score = terms[0]
    for term in terms[1:]:
        score = score + term
    return (
        df.with_columns((score / total_weight * 100.0).alias("zdk_score")),
        dropped,
    )


# ---------------------------------------------------------------------------
# 第 3 层：技术面确认 + 加分（zdk apply_technical）
# ---------------------------------------------------------------------------

def apply_technical(
    df: pl.DataFrame,
    technical: list[str],
    tech_bonus: dict[str, float] | None,
) -> pl.DataFrame:
    """第 3 层：技术面确认（硬淘汰）+ 加分（只影响排序）。

    zdk 用 ``strict_missing=True``：指标拿不到的标的直接淘汰 —— 早先为了让
    网络抖动不误杀而豁免缺失数据，结果 MACD 金叉策略选出根本没取到 K 线的票，
    选股完全失真。TSP 的 enriched 帧指标缺失即空值，这里同样按淘汰处理。
    """
    rules = [TECH_RULES[name] for name in (technical or []) if name in TECH_RULES]
    if rules:
        combined = rules[0]
        for expression in rules[1:]:
            combined = combined & expression
        df = df.filter(combined)

    bonus_terms: list[pl.Expr] = []
    for name, value in (tech_bonus or {}).items():
        expression = BONUS_RULES.get(name)
        if expression is None:
            expression = EXTRA_BONUS_RULES.get(name)
        if expression is None or not value:
            continue
        bonus_terms.append(pl.when(expression).then(float(value)).otherwise(0.0))
    if bonus_terms:
        bonus = bonus_terms[0]
        for term in bonus_terms[1:]:
            bonus = bonus + term
        df = df.with_columns(bonus.alias("zdk_tech_bonus"))
    else:
        df = df.with_columns(pl.lit(0.0).alias("zdk_tech_bonus"))
    return df


def apply_universe(df: pl.DataFrame, params: dict[str, Any] | None = None) -> pl.DataFrame:
    """zdk ``apply_prefilter`` 的通用可交易性过滤（第 1 层的前半段）。

    这是**打分之前**必须做的：zdk 的横截面百分位是以这层过滤后的全市场为
    分母的。如果把它挪到打分之后，每只票的百分位就变了，分数和排名都会漂。

    参数口径（``UNIVERSE_DEFAULTS``，可用 params 覆盖）：
    ``min_price`` / ``max_price`` 元、``min_turnover`` 亿元、``min_floatcap``
    / ``max_floatcap`` 亿元、``exclude_st``、``exclude_limit``。

    与 zdk 的唯一差异：``exclude_new``（剔除名称以 N/C 开头的新股）没有移植 ——
    TSP 的 instruments 名录里不存在这种交易所前缀（实测 5570 只里 0 命中），
    新股由引擎 ``DEFAULT_BASIC_FILTER.exclude_new_days`` 按上市天数兜住。
    """
    p = {**UNIVERSE_DEFAULTS, **(params or {})}
    conditions = [
        pl.col("close") >= float(p["min_price"]),
        pl.col("close") <= float(p["max_price"]),
        # zdk: turnover(万元) >= min_turnover*10000；TSP amount 是元
        pl.col("amount") >= float(p["min_turnover"]) * 1e8,
        # zdk: floatcap(亿元) >= min_floatcap
        pl.col("zdk_floatcap") >= float(p["min_floatcap"]),
    ]
    max_floatcap = float(p.get("max_floatcap") or 0.0)
    if max_floatcap > 0:
        conditions.append(pl.col("zdk_floatcap") <= max_floatcap)
    if p.get("exclude_st", True):
        conditions.append(
            ~pl.col("name").fill_null("").str.contains("(?i)ST|退")
        )
    if p.get("exclude_limit", True):
        conditions.append(pl.col("zdk_pct").abs() < 9.5)

    combined = conditions[0]
    for expression in conditions[1:]:
        combined = combined & expression
    return df.filter(combined.fill_null(False))


def apply_candidate_pool(df: pl.DataFrame, size: int) -> pl.DataFrame:
    """取每个交易日分数最高的 ``size`` 只，作为进入技术面确认的候选池。

    对应 zdk ``run_selection`` 里 ``cand = d.head(candidate_pool)``。它在
    **打分之后、技术面确认之前** —— 顺序反了会多拉明显不合格的票的日K，
    更关键的是让 ``apply_technical`` 的输入集合与 zdk 不一致。
    """
    if size <= 0 or df.is_empty():
        return df
    return (
        df.with_columns(
            pl.col("zdk_score").rank(method="ordinal", descending=True)
            .over("date").alias("zdk_rank")
        )
        .filter(pl.col("zdk_rank") <= size)
        .drop("zdk_rank")
    )


def apply_top_n(df: pl.DataFrame, top_n: int) -> pl.DataFrame:
    """按 ``zdk_final_score`` 取每交易日前 ``top_n`` 只（zdk 的 ``head(top_n)``）。"""
    if top_n <= 0 or df.is_empty():
        return df
    return (
        df.with_columns(
            pl.col("zdk_final_score").rank(method="ordinal", descending=True)
            .over("date").alias("zdk_pick_rank")
        )
        .filter(pl.col("zdk_pick_rank") <= top_n)
        .drop("zdk_pick_rank")
    )


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def run_spec(
    df: pl.DataFrame,
    spec: dict[str, Any],
    params: dict[str, Any] | None = None,
    enrich: Any = None,
) -> pl.DataFrame:
    """按 zdk 的三层漏斗执行一条策略定义，返回每交易日入选的 ``top_n`` 行。

    ``spec`` 与 zdk ``screener.STRATEGIES`` 的条目同构：
    ``{prefilter, weights, technical, tech_bonus, top_n, params?}``，
    额外支持 ``limit_pattern: True``（涨停形态类策略才需要）。

    ``params`` 是策略引擎解析后的用户参数（``META.params`` 的默认值 + 前端覆盖），
    优先级高于 ``spec["params"]``（策略内置默认），对齐 zdk 的
    ``p = {**DEFAULT_PARAMS, **params, **spec["params"]}`` 覆盖链。

    ``enrich`` 是可选的上下文补列回调 ``frame -> frame``，在 ``add_derived``
    之后、粗筛之前执行。「要信早信 / 心法框架」两条策略通过它注入市场宽度、
    题材共振、板块多头等依赖字面量快照的列；其余 22 条策略不需要。

    执行顺序严格对齐 zdk ``run_selection``::

        通用可交易性过滤 → 策略粗筛 → 横截面百分位打分
        → 取 candidate_pool → 技术面硬确认 + 加分 → head(top_n)

    注意与 TSP 引擎的配合：本函数返回的是「每个交易日已选好的票」，引擎随后
    再按 ``date == as_of`` 切片、跑 ``basic_filter`` 和自己的 ``scoring``。
    因此这里必须自己完成排名与截断 —— 引擎那一层的 ``limit`` 只是兜底显示上限。
    """
    if df is None or df.is_empty() or "date" not in df.columns:
        return df

    spec_params = spec.get("params") or {}
    merged = {**spec_params, **(params or {})}
    work = df.sort(["symbol", "date"])
    work = add_derived(work)
    # 「要信早信 / 心法框架」两条特殊策略要先补上下文列（市场宽度、题材共振、
    # 板块多头、3 日均额……），它们同时喂 prefilter 和 technical，所以必须在
    # 粗筛之前完成。为什么用回调而不是在本模块里 import 依赖表：
    # 引擎 `_load_file` 对同目录 `_*.py` 依赖文件做安全校验时只额外放行
    # {"collections.abc","types","typing"}，**不允许依赖之间互相 import**；
    # 只有策略文件本身才能 import 同目录依赖。所以表在这里由策略传进来。
    if enrich is not None:
        work = enrich(work)
    # 「涨停回马枪 / 涨停横盘」这两条策略的 technical 规则依赖 limit_pattern 列。
    # 自动开启，避免每个策略文件都要手写这个开关（漏写会在 filter 时报
    # ColumnNotFoundError: zdk_lp_ok）。
    needs_lp = bool(spec.get("limit_pattern")) or any(
        name in ("limit_pullback", "limit_sideways")
        for name in (spec.get("technical") or [])
    )
    if needs_lp:
        work = add_limit_pattern(work, LIMIT_PATTERN_LOOKBACK)

    work = apply_universe(work, merged)
    if work.is_empty():
        return work

    work = apply_prefilter(work, spec.get("prefilter") or {})
    if work.is_empty():
        return work

    work, _ = apply_scoring(
        work, spec.get("weights") or {}, spec.get("score_mode") or "percentile"
    )
    if work.is_empty():
        return work

    pool_size = merged.get("candidate_pool", UNIVERSE_DEFAULTS["candidate_pool"])
    work = apply_candidate_pool(work, int(pool_size or 0))
    if work.is_empty():
        return work

    work = apply_technical(
        work,
        spec.get("technical") or [],
        spec.get("tech_bonus") or {},
    )
    if work.is_empty():
        return work

    # zdk: final_score = (score + tech_bonus).round(2)
    work = work.with_columns(
        (pl.col("zdk_score") + pl.col("zdk_tech_bonus")).round(2)
        .alias("zdk_final_score")
    )
    return apply_top_n(work, int(spec.get("top_n") or 0))


def unsupported_notes(spec: dict[str, Any]) -> list[str]:
    """列出该策略里因 TSP 缺数据而被剔除的 zdk 因子（供 RULES / 文档说明）。"""
    notes: list[str] = []
    for key in (spec.get("prefilter") or {}):
        if key in UNSUPPORTED_PREFILTER:
            notes.append(f"prefilter.{key}")
    for key in (spec.get("weights") or {}):
        base = key[:-4] if key.endswith("_neg") else key
        if base in UNSUPPORTED_WEIGHTS:
            notes.append(f"weights.{key}")
    return notes
