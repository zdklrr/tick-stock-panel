"""框架·心法半医药（动态多头板块） — 移植自 zdk 选股策略软件。

原始定义：zdk `ths_strategy/framework.py` 的 `run()`（动态多头板块 → 成分池 →
九条限制 → `cum5*0.6 + amt3亿*0.4` 打分）。
策略说明：先找出「日线多头」的行业板块，只在多头板块的成分股里选票；
再用「KDJ 金叉状态 + 近 5 日累计上涨 + J 值不超买」三条过滤，
按「短期动能 + 成交容量」排序取票。

迁移方式：

* **板块多头判定** —— 完整复刻：`_zdk_common._board_bull_days` 用同花顺行业
  成分（`_zdk_boards` 快照）按总市值取每板块前 10 只构造等权指数，自算
  MA5 / MACD(12,26,9) / EXPMA12 / EXPMA50，连续 3 日三条件全满足即多头。
  zdk 的板块列表来自新浪/同花顺在线接口，本移植用随库分发的成分快照
  （`SNAPSHOT_DATE` 固定，新上市或调出行业的票会缺板块归属）。
* **九条限制** —— 全部复刻，`amt3` 用 `volume(手)*100*close(元)` 的 3 日均值
  （与 zdk 同近似，不依赖 vwap）。
* **两市总成交额** —— 用 enriched 帧当日全部标的的 amount 求和自算，
  替代 zdk 的 `data_feed.index_turnover_yi`；据此在 4.5~50 亿 / 4.5~70 亿
  两档之间切换 `amt3` 上限（zdk 的阈值 23000 亿原样保留）。
* 无板块归属的票（快照未覆盖）**直接淘汰**，不会报错。

执行后端：`python_history_legacy`（需要 12 根以上日K与 60 根指数历史，
回看 70 个交易日）。
"""

import _zdk_boards
import _zdk_common as zc
import polars as pl

EXECUTION_BACKEND = "python_history_legacy"

#: 板块等权指数要跑 EXPMA50 / MACD(26)，至少要 60 根日K；策略自身要 12 根。
#: 统一回看 70 个交易日（更快窗口会让 EMA50 尚未收敛）。
LOOKBACK_DAYS = zc.HOT_LOOKBACK_DAYS

#: 显式声明依赖列：除 ZDK_FEATURES 外还要 KDJ 三值与总股本（算板块等权指数
#: 的总市值权重、以及第 9 条 J 值上限）。不声明会导致回测层不载这些列。
REQUIRED_FEATURES = zc.ZDK_FEATURES | frozenset(
    {"kdj_k", "kdj_d", "kdj_j", "total_shares"}
)

META = {
    "id": "zdk_framework_semi_med",
    "name": "心法框架·动态多头板块",
    "description": (
        "动态多头行业板块内选票：板块等权指数连续 3 日多头 → KDJ 金叉 + 5 日"
        "上涨 + J≤110 → 按短期动能与成交容量排序"
    ),
    "tags": ["zdk移植", "框架"],
    # 与 zdk 一致的可交易性门槛，Stage1 复核一层（选股时已在漏斗里过滤过）。
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "amount_min": 1e8,
        "float_cap_min": 30e8,
        "exclude_st": True,
    },
    "params": {
        "candidate_pool": {"default": 800, "label": "候选池数量", "type": "int", "min": 20, "max": 4000, "step": 10, "group": "候选范围"},
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

#: zdk `framework.run` 的九条限制 + 打分公式。
#:
#: `score_mode="raw"` 直接复刻 `score = cum5 * 0.6 + (amt3 / 1亿) * 0.4`：
#: `zdk_cum5` 是近 5 日累计涨幅(%)，`zdk_amt3_yi` 是 3 日均额(亿元)。
SPEC: dict = {
    "enrich": "framework",
    "score_mode": "raw",
    "prefilter": {},
    "weights": {"cum5": 0.6, "amt3": 0.4},
    "technical": [
        "fw_board_bull",   # 第1步：所在行业板块连续 3 日多头
        "fw_bars_ok",      # 限制①：日K >= 12 根
        "fw_mv_ok",        # 限制②：总市值 > 0 且 < 1000 亿
        "fw_pct10_ok",     # 限制④：近十日涨幅 <= 95%
        "fw_lu5_ok",       # 限制⑤：近五日涨停数 < 4
        "fw_ow20_ok",      # 限制⑥：近二十日无一字板
        "fw_amt3_ok",      # 限制⑦：amt3 落在当日档位带内
        "fw_cum5_ok",      # 限制⑧：cum5 > 0
        "fw_kdj_ok",       # 第2步 + 限制⑨：K > D 且 J <= 110
        "fw_not_limit",    # 今日涨停剔除（买不进）
    ],
    "tech_bonus": {},
    "top_n": 10,
    "params": {"exclude_limit": False, "candidate_pool": 800},
}

ENTRY_SIGNALS: list = []
EXIT_SIGNALS: list = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5

RULES = """
1. 全市场通用可交易性过滤（对齐 zdk `DEFAULT_PARAMS`）：股价 3~300 元、成交额 ≥ 1 亿元、流通市值 ≥ 30 亿元、剔除 ST/退市、**不剔除涨停板**（涨停票在后面单独剔除）。
2. 第 1 步 板块多头：按同花顺行业成分，每板块取总市值前 10 只构造等权指数（成分数 < 5 的板块不参与），自算 MA5 / MACD(12,26,9) / EXPMA12 / EXPMA50；连续 3 个交易日同时满足「收盘 > MA5 ｜ DIF > DEA ｜ EMA12 > EMA50」即多头。候选池 = 多头板块成分并集。
3. 第 2 步 入选三条件：KDJ 处于金叉状态（K > D）、近 5 日累计涨幅 > 0（`cum5`）、J 值 ≤ 110（不超买）。
4. 九条限制（不满足直接淘汰）：①日K ≥ 12 根；②总市值 > 0 且 < 1000 亿；③剔除 ST；④近十日涨幅 `(昨收/11日前收 − 1) × 100 ≤ 95%`；⑤近五日涨停数 < 4（涨停阈值 30/68/69 开头 19.5%，其余 9.8%）；⑥近二十日出现过一字板则剔除；⑦3 日均额 `amt3` 落在当日档位带内 —— 两市总成交额 < 23000 亿时要求 4.5 亿 < amt3 < 50 亿，≥ 23000 亿时要求 4.5 亿 < amt3 < 70 亿；⑧`cum5` 分母（6 日前收盘）有效；⑨J ≤ 110。另外今日涨停的票直接剔除（买不进）。
5. 排序：`score = cum5 × 0.6 + amt3(亿元) × 0.4`，按分数从高到低取前 10 只。
6. **数据降级（必须知晓）**：
   - zdk 的板块列表与成分来自新浪/同花顺在线接口（49 个节点、7 天缓存）；TSP 策略运行环境不允许读磁盘/联网，改用随库分发的同花顺行业成分快照（`_zdk_boards.SNAPSHOT_DATE`）。快照未覆盖的票（新上市、被调出行业）**无法判定板块归属，直接淘汰**；快照不更新会导致板块成分与实际略有偏差。
   - zdk 的「两市总成交额」取自上游指数数据；TSP 侧用当日全部标的 amount 求和自算，口径接近但非严格同源，档位切换日可能偶有差异。
   - zdk 的板块指数成分按「板块内成交额前 N」构造，本移植按「总市值前 10 只」构造（TSP 缺板块级成交额排名），指数走势略有差异。
7. 与 zdk 一致：本策略的价值因子（`cum5`/`amt3`）与九条限制在 TSP 指标表里都有对应列，未做删减；`kdj_k/kdj_d/kdj_j` 由 TSP 指标管道按同一口径（RSV 9 日、K/D 递推系数 2/3、`J = 3K − 2D`）计算。
"""


def _enrich(frame: pl.DataFrame) -> pl.DataFrame:
    """注入总市值、cum5、3 日均额、板块多头天数与九条限制的判定列。"""
    return zc.add_framework_context(frame, _zdk_boards.industry_map())


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """按 zdk「心法框架」的动态多头板块漏斗选出每个交易日的前 10 只。

    返回每个交易日各自的入选行（不是只返回最新一天），回测引擎需要全区间命中。
    """
    return zc.run_spec(df, SPEC, params, enrich=_enrich)
