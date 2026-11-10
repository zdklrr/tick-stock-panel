"""游资·要信早信 — 移植自 zdk 选股策略软件。

原始定义：zdk `ths_strategy/yaoxin.py` 的 `run()`（六道闸门 + 仓位排序）。
策略说明：高位连板梯队集体断板（换节点）的当天，情绪还没退潮的阶段，
只做「今日首板 + 位置不高 + 有趋势 + 有容量 + 有题材共振」的低位接力票。

迁移方式：同花顺涨停池（`ztpool`）取不到，六道闸门里依赖它的三条按下面降级，
其余三条（断板触发 / 同节点低位 / 容量 / 趋势）用 enriched 日K完整复刻：

* **⑥ 情绪闸门** —— 完全可复刻：全市场涨停/炸板/跌停家数与最高连板都由
  `_zdk_common._market_breadth` 按交易日自算，情绪阶段判定顺序与 zdk
  `hotmoney.stage_of` 逐条一致。
* **⑤ 辨识度打分** —— 部分降级：`first_limit_up_time`（首封时间，+30 分）与
  `open_num`（开板次数，+20 分）在日K层面不可得，**这两项恒为 0**；同题材
  涨停家数（+min(tn,10)*3）用同花顺概念成分的「同概念当日涨停家数」代理。
* **② 节点起点** —— 完整复刻：断板高标各自连板段起点**日期**取最小值作节点日，
  再取候选自己在该日的收盘价算节点涨幅（与 zdk `_judge_common` 的
  `hc[-1]/hc[jn]-1` 同口径）。

分值上限因此是 50 + 0 + 0 + 30 + 12 = 92（zdk 侧是 142），排序语义不变。

执行后端：`python_history_legacy`（需要多日窗口重建连板梯队，回看 70 个交易日）。
"""

import _zdk_common as zc
import _zdk_concepts
import polars as pl

EXECUTION_BACKEND = "python_history_legacy"

#: 连板梯队要回溯 20 日、节点涨幅要回溯 20 日、趋势确认要 10 日，
#: 统一回看 70 个交易日（与 zdk `days_back=90` 自然日大致同量级）。
LOOKBACK_DAYS = zc.HOT_LOOKBACK_DAYS

#: 显式声明依赖列，回测才不会退化成全指标计算。
REQUIRED_FEATURES = zc.ZDK_FEATURES

META = {
    "id": "zdk_yaoxin",
    "name": "要信早信",
    "description": (
        "高位连板集体断板换节点的当天，在情绪未退潮阶段做今日首板的低位接力"
        "（断板触发 + 同节点低位 + 容量 + 趋势确认 + 辨识度打分 + 情绪闸门）"
    ),
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
        "candidate_pool": {"default": 500, "label": "候选池数量", "type": "int", "min": 20, "max": 4000, "step": 10, "group": "候选范围"},
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

#: zdk `yaoxin.run` 的逐段复刻。
#:
#: `score_mode="raw"` 表示不走横截面百分位，直接按 zdk 的加法公式累加：
#: `base*1.0 + theme_score*1.0 + node_lo*0.8`
#:   - `zdk_base_score`   = 50.0            （zdk 保底分）
#:   - `zdk_theme_score`  = min(tn,10)*3    （tn = 同概念当日涨停家数）
#:   - `zdk_node_lo`      = max(0, 15-node_gain)
#: 首封早（+30）与炸板回封（+20）两项因缺数据恒为 0（见 RULES 第 8 条）。
SPEC: dict = {
    "enrich": "yaoxin",
    "score_mode": "raw",
    "prefilter": {},
    "weights": {"base": 1.0, "theme_score": 1.0, "node_lo": 0.8},
    "technical": [
        "yx_trigger",      # ① 断板触发：昨日最高连板 >= 4 且高标全部断板
        "yx_stage_ok",     # ⑥ 情绪闸门：昨日阶段 ∈ {低位试错, 高位震荡}
        "yx_first_board",  # ② 今日首板（今日涨停且连板数 <= 1）
        "yx_node_low",     # ② 同节点低位：节点以来涨幅 <= 15%
        "yx_gain20_ok",    # ② 近 20 日累计涨幅 <= 40%
        "yx_trend_ok",     # ④ 趋势确认：前 10 交易日涨幅 ∈ [15%, 50%]
        "yx_amount_ok",    # ③ 容量闸：当日成交额 >= 3 亿元
    ],
    "tech_bonus": {},
    "top_n": 10,
    "params": {"exclude_limit": False, "candidate_pool": 500},
}

ENTRY_SIGNALS: list = []
EXIT_SIGNALS: list = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5

RULES = """
1. 全市场通用可交易性过滤（对齐 zdk `DEFAULT_PARAMS`）：股价 3~300 元、成交额 ≥ 1 亿元、流通市值 ≥ 30 亿元、剔除 ST/退市、**不剔除涨停板**（本策略选的正是今日首板）。
2. ① 断板触发（硬性淘汰）：昨日全市场最高连板数 ≥ 4（`min_streak`），且昨日这批最高连板的高标**今日全部断板**（`break_ratio=1.0`）。没有高标断板就不出票。
3. ⑥ 情绪闸门（硬性淘汰）：昨日情绪阶段必须属于「低位试错(trial)」或「高位震荡(high_chaos)」。阶段判定按 zdk `hotmoney` 口径，用**占全市场只数的百分比**：跌停占比 ≥ 0.25% 或 炸板率 ≥ 45% 且涨停占比 < 0.6% → 主跌期；涨停占比 ≥ 2% 且最高连板 ≥ 4 且炸板率 < 28% → 主升期；涨停占比 ≥ 1.2% 且炸板率 ≥ 32% → 高位震荡；涨停占比 < 0.6% → 冰点期；其余 → 低位试错。
4. ② 今日首板（硬性淘汰）：今日涨停且连板数 ≤ 1（即昨日未涨停）。同节点低位：断板高标各自连板段起点收盘价的最小值算起，当前（昨日收盘）涨幅 ≤ 15%。近 20 日累计涨幅 ≤ 40%。
5. ④ 趋势确认（硬性淘汰）：前 10 交易日涨幅 ∈ [15%, 50%]。低于下限说明没有趋势，高于上限说明主升已走完。
6. ③ 容量闸（硬性淘汰）：今日成交额 ≥ 3 亿元。
7. 打分（`score_mode="raw"`，zdk 加法公式）：保底 50 分 + 同题材涨停家数分（同概念当日涨停家数 tn ≥ 3 时给 `min(tn,10)*3` 分，最多 30 分）+ 位置分（`max(0, 15 - 节点涨幅%) * 0.8`；节点涨幅越负分越高，与 zdk 原语义一致，这一项没有上界）。按最终分取前 10 只。
8. **数据降级（必须知晓）**：zdk 原打分里还有两项在 TSP 日K层面完全不可得，本实现恒为 0 分：
   - 「首封时间早于 10:30」+30 分 —— TSP 无涨停池首封时间 `first_limit_up_time`；
   - 「炸板回封、换手 ≤ 30%、开板 ≤ 4 次」+20 分 —— TSP 无开板次数 `open_num`。
   因此本地分值在「节点涨幅 ≥ 0」时满分是 92（zdk 是 142），选股会更依赖「位置 + 题材共振」两项；若日后 TSP 接入涨停池分时数据，把这两项按 zdk 原值补回即可。
9. 其它降级：题材家数用「同花顺概念成分里当日涨停家数」代理 zdk 涨停池的 `reason_type`；情绪阶段的基准只数用帧内当日实际标的数（约 5570）代替 zdk 写死的 4600，分母略大、闸门略紧。
"""


def _enrich(frame: pl.DataFrame) -> pl.DataFrame:
    """注入市场宽度、情绪阶段、节点涨幅、题材共振与三档打分明细列。"""
    return zc.add_yaoxin_context(frame, _zdk_concepts.concept_map())


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """按 zdk「要信早信」的六道闸门选出每个交易日的前 10 只。

    返回每个交易日各自的入选行（不是只返回最新一天），回测引擎需要全区间命中。
    """
    return zc.run_spec(df, SPEC, params, enrich=_enrich)
