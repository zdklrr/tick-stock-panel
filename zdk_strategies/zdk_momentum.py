"""动量强势 — 移植自 zdk 选股策略软件。

原始定义：zdk `ths_strategy/screener.py` 的 `STRATEGIES["momentum"]`（engine=generic）。
策略说明：中期动量向上 + 主力资金净流入，赚趋势延续的钱

迁移方式：三层漏斗（粗筛 → 横截面百分位打分 → 技术面确认）原样保留，
字段按 `_zdk_common` 的映射表换到 TSP 的 enriched 指标列。
执行后端：`python_history_legacy`（需要多日窗口，回看 70 个交易日）。
"""

import polars as pl

import _zdk_common as zc

EXECUTION_BACKEND = "python_history_legacy"

#: 需要 60 日动量 / 60 日新高 / 20 日涨停统计，统一回看 70 个交易日。
LOOKBACK_DAYS = zc.HOT_LOOKBACK_DAYS

#: 显式声明依赖列，回测才不会退化成全指标计算。
REQUIRED_FEATURES = zc.ZDK_FEATURES

META = {
    "id": "zdk_momentum",
    "name": "动量强势",
    "description": "中期动量向上 + 主力资金净流入，赚趋势延续的钱",
    "tags": ["zdk移植", "进攻"],
    # 与 zdk 一致的可交易性门槛，Stage1 复核一层（选股时已在漏斗里过滤过）。
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "amount_min": 1e8,
        "float_cap_min": 30e8,
        "exclude_st": True,
    },
    "params": {
        "candidate_pool": {"default": 200, "label": "候选池数量", "type": "int", "min": 20, "max": 2000, "step": 10, "group": "候选范围"},
        "exclude_limit": {"default": True, "label": "剔除涨跌停板", "type": "bool", "group": "候选范围"},
        "min_price": {"default": 3.0, "label": "最低股价(元)", "type": "float", "min": 0.0, "max": 100.0, "step": 0.5, "group": "可交易性门槛"},
        "max_price": {"default": 300.0, "label": "最高股价(元)", "type": "float", "min": 10.0, "max": 2000.0, "step": 10.0, "group": "可交易性门槛"},
        "min_turnover": {"default": 1.0, "label": "最低成交额(亿元)", "type": "float", "min": 0.0, "max": 50.0, "step": 0.5, "group": "可交易性门槛"},
        "min_floatcap": {"default": 30.0, "label": "最低流通市值(亿元)", "type": "float", "min": 0.0, "max": 2000.0, "step": 10.0, "group": "可交易性门槛"},
        "exclude_st": {"default": True, "label": "剔除 ST/退市", "type": "bool", "group": "可交易性门槛"},
    },
    # 打分已在 filter_history 内按 zdk 口径完成，这里不再声明 scoring，
    # 否则引擎会再做一次 min-max 归一化并覆盖排序字段。
    "order_by": "zdk_final_score",
    "descending": True,
    "limit": 20,
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}

#: zdk 策略定义的逐字段复刻（prefilter / weights / technical / tech_bonus / top_n）。
#: 带 `_neg` 后缀的权重表示「越小越好」；`main*` / `pe_*` 在 TSP 无对应数据，
#: 由 _zdk_common.apply_scoring 剔除后按剩余权重重新归一化。
SPEC: dict = {
    "prefilter": {"d20_min": 0, "d60_min": 0, "main_min": 0},
    "weights": {"d20": 0.3, "d10": 0.18, "main_ratio": 0.22, "lb": 0.15, "hsl": 0.15},
    "technical": ["above_ma20"],
    "tech_bonus": {"bull_stack": 8, "new_high20": 6, "macd_gold": 5},
    "top_n": 20,
}

ENTRY_SIGNALS: list = []
EXIT_SIGNALS: list = []
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20

RULES = """
1. 全市场通用可交易性过滤（对齐 zdk `DEFAULT_PARAMS`）：股价 3~300 元、成交额 ≥ 1 亿元、流通市值 ≥ 30 亿元、剔除 ST/退市、剔除涨跌停板。
2. 粗筛（策略专属门槛）：20日涨幅下限(%)满足 `d20_min=0`；60日涨幅下限(%)满足 `d60_min=0`。
3. 横截面百分位打分（0~100，分母是粗筛后的全市场）：`d20` 权重 0.3 → 20日涨幅；`d10` 权重 0.18 → 10日涨幅；`lb` 权重 0.15 → 量比；`hsl` 权重 0.15 → 换手率(%)。
4. 技术面硬性确认（不满足直接淘汰）：收盘站上 MA20。
5. 技术面加分（只影响排序，不淘汰）：均线多头排列(MA5>MA10>MA20 且收盘≥MA5) +8；创 20 日新高(最高价口径) +6；MACD 近 3 日金叉 +5。
6. 最终分 = 打分 + 加分，按最终分取前 20 只。
7. 与 zdk 原策略的差异：zdk 版本还用了 `prefilter.main_min`、`weights.main_ratio`，这些字段 TSP 没有对应数据（主力资金需付费扩展源、市盈率未并入指标表），已剔除并按剩余权重重新归一化，排序口径会与原版略有不同。
"""


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """按 zdk「动量强势」的三层漏斗选出每个交易日的前 20 只。

    返回每个交易日各自的入选行（不是只返回最新一天），回测引擎需要全区间命中。
    """
    return zc.run_spec(df, SPEC, params)
