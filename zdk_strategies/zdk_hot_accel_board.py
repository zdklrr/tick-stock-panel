"""游资·加速新高板 — 移植自 zdk 选股策略软件。

原始定义：zdk `ths_strategy/screener.py` 的 `STRATEGIES["hot_accel_board"]`（engine=generic）。
策略说明：封板同时创 60 日新高（最高价口径，上方无套牢盘），且只接首板/一进二（陈小群「四种有效涨停」之加速型）

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
    "id": "zdk_hot_accel_board",
    "name": "游资·加速新高板",
    "description": "封板同时创 60 日新高（最高价口径，上方无套牢盘），且只接首板/一进二（陈小群「四种有效涨停」之加速型）",
    "tags": ["zdk移植", "游资"],
    # 与 zdk 一致的可交易性门槛，Stage1 复核一层（选股时已在漏斗里过滤过）。
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "amount_min": 1e8,
        "float_cap_min": 30e8,
        "exclude_st": True,
    },
    "params": {
        "candidate_pool": {"default": 300, "label": "候选池数量", "type": "int", "min": 20, "max": 2000, "step": 10, "group": "候选范围"},
        "exclude_limit": {"default": False, "label": "剔除涨跌停板", "type": "bool", "group": "候选范围"},
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
    "limit": 10,
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}

#: zdk 策略定义的逐字段复刻（prefilter / weights / technical / tech_bonus / top_n）。
#: 带 `_neg` 后缀的权重表示「越小越好」；`main*` / `pe_*` 在 TSP 无对应数据，
#: 由 _zdk_common.apply_scoring 剔除后按剩余权重重新归一化。
SPEC: dict = {
    "prefilter": {"pct_min": 9.5, "turnover_min": 10000, "hsl_min": 2.5},
    "weights": {"turnover": 0.35, "hsl": 0.3, "d20": 0.35},
    "technical": ["hot_accel_board"],
    "tech_bonus": {"above_ma20": 6, "vol_ratio5_hi": 6},
    "top_n": 10,
    "params": {"exclude_limit": False, "candidate_pool": 300},
}

ENTRY_SIGNALS: list = []
EXIT_SIGNALS: list = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5

RULES = """
1. 全市场通用可交易性过滤（对齐 zdk `DEFAULT_PARAMS`）：股价 3~300 元、成交额 ≥ 1 亿元、流通市值 ≥ 30 亿元、剔除 ST/退市、不剔除涨跌停板。
2. 粗筛（策略专属门槛）：当日涨幅下限(%)满足 `pct_min=9.5`；成交额下限(万元)满足 `turnover_min=10000`；换手率下限(%)满足 `hsl_min=2.5`。
3. 横截面百分位打分（0~100，分母是粗筛后的全市场）：`turnover` 权重 0.35 → 成交额(万元)；`hsl` 权重 0.3 → 换手率(%)；`d20` 权重 0.35 → 20日涨幅。
4. 技术面硬性确认（不满足直接淘汰）：游资加速新高板形态。
5. 技术面加分（只影响排序，不淘汰）：收盘站上 MA20 +6；量比 ≥ 1.5(放量) +6。
6. 最终分 = 打分 + 加分，按最终分取前 10 只。
7. 与 zdk 原策略一致：所有因子在 TSP 指标表里都有对应列，权重未做删减。
"""


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """按 zdk「游资·加速新高板」的三层漏斗选出每个交易日的前 10 只。

    返回每个交易日各自的入选行（不是只返回最新一天），回测引擎需要全区间命中。
    """
    return zc.run_spec(df, SPEC, params)
