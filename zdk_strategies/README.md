# zdk 选股策略 → TSP 移植说明

把 `G:\zdk选股策略软件` 里的策略搬到 TSP 里跑，**zdk 本身一行代码没改**（只做 AST 静态读取，
从未 import 它的任何模块），TSP 源码也没有改——TSP 的策略引擎本来就设计成加载用户自定义
Python 文件，这里只是往自定义目录里放文件。

## 一、装在哪

| 位置 | 用途 |
| --- | --- |
| `C:\Users\jiangliuer\AppData\Local\Programs\TSP\data\strategies\custom\` | **实际生效位置**，TSP 启动时扫描 |
| `G:\tick_stock_panel\zdk_strategies\` | 源文件与版本备份，改完拷过去 |

28 个文件 = `_zdk_common.py`（共享库，约 1660 行）+ `_zdk_boards.py` / `_zdk_concepts.py`
（同花顺行业与概念成分快照，随库分发）+ 24 个策略文件 + 本 README。以下划线开头的文件
TSP 引擎不会把它们当策略，但同目录的策略可以 `import` 它们。

装完在客户端里点「重新加载策略」，或调 `POST /api/strategies/reload`。实测返回
`{"ok":true,"count":24}`。

## 二、移植了哪 24 个

zdk 的 `screener.py:43` 里登记了 26 个策略，其中 24 个搬过来了：

- 均衡/趋势类 11 个：`multifactor` `momentum` `breakout` `pullback` `reversal` `leading`
  `macd` `oversold` `lowvol` `volume_price` `intraday_strong`
- 涨停形态 2 个：`limit_pullback` `limit_sideways`
- 游资手法 9 个：`hot_first_board` `hot_relay` `hot_weak2strong` `hot_leader_yin`
  `hot_ma_pullback` `hot_reverse_board` `hot_second_wave` `hot_low_replace` `hot_accel_board`
- 特殊两条 2 个：`yaoxin`（要信早信）、`framework_semi_med`（心法框架）

**没搬的 2 个**，各有明确原因：

| 策略 | 原因 |
| --- | --- |
| `seal_strength` | 需要实时五档盘口逐笔委买强度，选股用的日线截面算不出来 |
| `mainforce` | 权重里 65% 是主力资金（`main_ratio`/`main5_ratio`），TSP 没有资金流数据；剩下的因子不足以支撑一个「主力资金」策略，强行移植会名不副实 |

### 2.1 两条特殊策略的降级口径

`yaoxin` 与 `framework_semi_med` 都依赖 zdk 的实时涨停池 / 在线板块接口，TSP 侧拿不到，
按下面降级（详细写在各自文件头 docstring 与 `RULES` 里）：

| 策略 | zdk 依赖 | TSP 处理 |
| --- | --- | --- |
| `zdk_yaoxin` | 涨停池首封时间 `first_limit_up_time` | **恒 0 分**（原 +30 分），日K层面不可得 |
| | 开板次数 `open_num` | **恒 0 分**（原 +20 分），日K层面不可得 |
| | 涨停池题材 `reason_type` | 用同花顺概念的「同概念当日涨停家数」代理 |
| | 情绪阶段（`hotmoney.stage_of`） | **完整复刻**，全市场涨停/炸板/跌停家数与最高连板按日自算 |
| `zdk_framework_semi_med` | 新浪/同花顺在线板块成分 | 用随库分发的 `_zdk_boards.py` 同花顺行业成分快照 |
| | 板块指数日K | 按总市值取每板块前 10 只构造等权指数，自算 MA5/MACD/EXPMA |
| | `data_feed.index_turnover_yi` | 用当日全部标的 `amount` 求和自算 |

两条策略都用 `python_history_legacy` 后端，回看 70 个交易日。

**为什么板块表要固化成 `.py` 字面量**：TSP 的策略安全沙箱（`ai_generator._validate_safety`）
禁止 `pl.read_parquet` / `Path` / `open` 等一切磁盘访问，`market_data.get_ext_timeseries()`
又只读 `ext_data/{id}/timeseries/` 分区，而 `ext_hy_ths` / `ext_gn_ths` 是 snapshot 模式
（`part.parquet` 直接放在 `ext_data/{id}/` 下），实测返回空 DataFrame。所以只能用
`_probe\gen_zdk_boards.py` 从真实扩展表导出成纯字面量模块，随策略库一起分发。
概念表用 base36 双字符下标编码压到 168KB（明文字面量要 595KB，会被每个策略加载时重复校验）。

## 三、口径怎么对齐的

zdk 是 pandas 逐行算，TSP 是 polars 向量化，单位定义也不一样。全部换算集中在
`_zdk_common.py`，策略文件里只写参数，不碰口径。

### 3.1 单位

| 概念 | zdk | TSP | 换算 |
| --- | --- | --- | --- |
| 涨跌幅 | 百分数 `pct=5.2` | 小数 `change_pct=0.052` | `×100` |
| N 日涨幅 | 百分数 | `momentum_Nd` 小数 | `×100` |
| 振幅 | 百分数 | `amplitude` 小数 | `×100` |
| 换手率 | 百分数 | `turnover_rate` 百分数 | 直接用 |
| 成交额 | 万元 | `amount` 元 | `×10000` |
| 流通市值 | 亿元 | `close*float_shares` 元 | `/1e8` |
| 量比 | 6 日均量 | `vol_ratio_5d` 前 5 日均量 | 口径略有差异，见 3.4 |

### 3.2 涨跌停价：跟 TSP 的交易所规则走，并修掉 zdk 的三处过时规则

zdk 自己算涨停价有三个问题，2026 年的市场已经不适用了。TSP 的
`backend/app/price_limits.py` 是交易所规则的单一事实源，移植时以它为准：

1. **北交所按代码首位 `8`/`4` 判断是错的。** 北交所现在用 `920xxx` 代码，实测 354 只
   一只都匹配不到，全被当成 10% 主板，30% 的涨停全部漏判。改成按 `.BJ` 后缀识别。
2. **主板 ST 自 2026-07-06 起涨跌幅是 10%，不再是 5%。** zdk 无条件按 5%，会把
   `*ST西旅` 涨 8.81% 误判成涨停。现在按 `MAIN_BOARD_ST_LIMIT_CHANGE_DATE` 分界处理；
   创业板/科创板的 ST 一直是 20%，不受影响。
3. **创业板前缀要含 `302`**，zdk 只写了 300/301/688/689。

涨停价用整数分算术复刻交易所的进位规则（`(cents*numerator+50)//100`），不是简单的
`round(prev*1.1, 2)`——低价股这两者结果不同。

判定用 `close >= 涨停价 - 0.005`（交易所口径的容差），不是 zdk 的「涨幅 ≥ 9.6%」近似。

**验证**：2026-11-02 判出 132 只涨停，其中 **132 只的收盘价精确等于算出的涨停价（100%）**，
反向检查「收盘价等于涨停价却没被判为涨停」= **0 只**。近 20 个交易日每日涨停数 32~141
（均值 67.9），符合 A 股常态。zdk 原口径判 146 只，多出的 14 只是上面前两条规则造成的误判。

> 注：TSP 自带的 `limit_up_price` 列在最新交易日会与它自己的 `prev_close` 差一天
> （实测 601099.SH：`prev_close=3.22` 应得 3.54，该列报 3.545）。所以这里不依赖它，
> 自己算。

### 3.3 三层漏斗的顺序保真

zdk 的顺序是：**通用过滤 → 粗筛 → 横截面打分 → 取候选池 → 技术面淘汰 → 取前 N**。

这个顺序不能动。横截面打分用的是百分位排名，分母是「过滤后剩下的全市场」——如果先打分
再过滤，每只票的百分位、分数、名次会全部漂移。候选池截断也必须在技术面确认之前，否则
技术面淘汰掉一部分后候选池会变空。

`_zdk_common.py` 的 `run_spec()` 严格照这个顺序：`add_derived → [enrich] → apply_universe →
apply_prefilter → apply_scoring → apply_candidate_pool → apply_technical → apply_top_n`。

方括号里的 `enrich` 是给两条特殊策略加的钩子（`run_spec(df, spec, params, enrich=fn)`），
在派生列算完、通用过滤之前注入板块/情绪/题材上下文。**必须是回调**——`_zdk_common.py`
自己不能 `import` 同目录的 `_zdk_boards` / `_zdk_concepts`（TSP 对依赖文件的 AST 校验只
放行 polars/numpy/datetime/typing 等少数模块），只有策略文件能 import 依赖。

所以这些策略声明的是 `python_history_legacy` 后端——它在 `filter_history()` 里把整套
漏斗跑完，返回「每个交易日已经选好的票」，TSP 引擎再从中切出目标日期。

### 3.4 已知的口径差异（不影响可用性，但要知道）

- **量比**：zdk 是 `volume / volume.rolling(6).mean().shift(1)`（约 6 日均量），TSP 的
  `vol_ratio_5d` 是前 5 日均量。数值会差几个百分点，不改变排序方向。
- **`exclude_new`（排除次新）**：zdk 按名称 `^[NC]\s?\S` 排，实测 TSP 的美股名录里
  **0 个命中**，等于没起作用。改用 TSP 引擎自带的 `exclude_new_days=30`。
- **权重里被剔掉的因子**：见下节。

## 四、缺数据的因子怎么处理的

zdk 的因子里有四个 TSP 没有对应数据，移植时**剔除并按剩余权重重新归一**（不是补 0，
补 0 会把分数压低）：

| 因子 | 缺什么 | 影响的策略 |
| --- | --- | --- |
| `main` / `main_ratio` / `main5_ratio` / `main5_net` | TSP 没有主力资金流向数据 | 除 `mainforce`（整个跳过）外，其余策略的这类权重被剔除 |
| `pe` / `pe_low` | 财务数据未 JOIN 进日线 | `lowvol`、`multifactor` 的估值权重被剔除 |

剔除后权重自动归一，所以总分还是 0~100 的量级，只是打分的**侧重点变了**。每个策略文件的
`RULES` 里都写明了这条策略具体剔了哪些因子。

实测剔除比例：
- `multifactor` 剔 `main_ratio`(0.20) + `pe_low`(0.08) = 28% 权重，剩 72% 归一
- `lowvol` 剔 `main_ratio`(0.20) + `pe_low`(0.15) = 35%
- `hot_*` 系列只剔 `main_*`，剩 60~85%
- `breakout` `oversold` `lowvol` 等不依赖资金流，剔除后差异最小

## 五、实测结果（2026-11-02 收盘数据，真实客户端 API）

24 个策略跑完 **44.3 秒，0 失败**。回归对比：与本次改动前的基线 `_probe\run_all2.log`
逐策略比对，**22 个旧策略的结果数全部一致**，只有新增的两条产生新条目。

| 策略 | 结果数 | 耗时 | 前三名 |
| --- | --- | --- | --- |
| `zdk_breakout` | 20 | 2.2s | 今世缘 / 迎驾贡酒 / 巴比食品 |
| `zdk_framework_semi_med` | 5 | 2.0~3.8s | 古井贡酒 / 迎驾贡酒 / 龙江交通 |
| `zdk_hot_accel_board` | 7 | 1.7s | 襄阳轴承 / 华是科技 / 泉阳泉 |
| `zdk_hot_first_board` | 10 | 1.7s | 银之杰 / 捷成股份 / 赢时胜 |
| `zdk_hot_leader_yin` | **0** | 1.9s | —（见下） |
| `zdk_hot_low_replace` | 10 | 2.0s | 银之杰 / 捷成股份 / 赢时胜 |
| `zdk_hot_ma_pullback` | 10 | 2.2s | 凡拓数创 / 大洋电机 / 百洋股份 |
| `zdk_hot_relay` | 4 | 2.5s | 泉阳泉 / 沈阳机床 / 合百集团 |
| `zdk_hot_reverse_board` | 10 | 1.6s | 银之杰 / 捷成股份 / 赢时胜 |
| `zdk_hot_second_wave` | 10 | 1.6s | 果麦文化 / 国芳集团 / 润建股份 |
| `zdk_hot_weak2strong` | **0** | 1.6s | —（见下） |
| `zdk_intraday_strong` | 20 | 1.6s | 许继电气 / 迎驾贡酒 / 威高血净 |
| `zdk_leading` | 15 | 1.6s | 上海沪工 / 光洋股份 / 羚锐制药 |
| `zdk_limit_pullback` | 6 | 2.1s | 兆威机电 / 大名城 / 豪鹏科技 |
| `zdk_limit_sideways` | 20 | 2.2s | 兆威机电 / 慈星股份 / 科远智慧 |
| `zdk_lowvol` | 20 | 1.4s | 中国石化 / 中国神华 / 中国电信 |
| `zdk_macd` | 20 | 1.2s | 巴比食品 / 羚锐制药 / 新媒股份 |
| `zdk_momentum` | 20 | 1.1s | 上海沪工 / 盛洋科技 / 巴比食品 |
| `zdk_multifactor` | 20 | 1.3s | 光洋股份 / 盛洋科技 / 上海沪工 |
| `zdk_oversold` | 20 | 2.4s | 臻宝科技 / 东芯股份 / 东山精密 |
| `zdk_pullback` | 20 | 1.8s | 福建水泥 / 百亚股份 / 德尔股份 |
| `zdk_reversal` | 20 | 1.8s | 东山精密 / 益方生物-U / 万润新能 |
| `zdk_volume_price` | 20 | 1.8s | 光线传媒 / 新媒股份 / 佳讯飞鸿 |
| `zdk_yaoxin` | **0** | 2.6~11.0s | —（见下） |

### 三个 0 结果的策略不是坏了

- **`hot_weak2strong`（弱转强反包）**：要求「昨天炸板 + 今天涨停」。当日全市场涨停 132 只，
  其中**昨天炸板的只有 1 只**，再叠加「非一字 + 可买入」后归零。这是行情本身没有这种形态，
  条件收紧是对的——硬凑出结果反而是错的。
- **`hot_leader_yin`（龙头首阴低吸）**：要求近期强票缩量收阴。当日「近 10 日 ≥2 次涨停」
  74 只，其中收阴的 18 只，站稳 MA10 的 11 只，但**再要求量比 ≤0.9 就一只不剩**——当天这批
  票都没有明显缩量。同样是行情未到位。
- **`yaoxin`（要信早信）**：六道闸门的第一道就没过。当日（2026-11-02）全市场**最高连板只有
  5 板且没有高标断板**，情绪阶段判为 `decline`（主跌期），而策略只在 `trial` / `high_chaos`
  放行。数据侧的逐级拆解：可交易 2636 只 → `yx_trigger`（断板触发）**0 只**，后面五道门都
  是 0 进 0 出，所以 `total=0` 是行情原因，不是代码错。

这三个策略换一个交易日就会有结果，不需要调参数。

`yaoxin` 的代码本身是**能出票**的，已用全区间回放验证（`_probe\p10_yaoxin.py`）：
- 70 个交易日里，断板触发日 27 天、情绪放行日 39 天、**两者同时成立 19 天**
- 这 19 天里逐级漏斗都有票，例如 2026-08-14 `可交易 2487 → 首板 40 → 节点低 39 → 20日 39
  → 趋势 27 → 容量 23`
- `filter_history` 全区间命中 **22 行 / 6 个交易日**（2026-08-14、08-17、08-21、09-03、
  09-08、09-23），样本：2026-09-08 华锦股份 94.63 / 香江控股 85.52 / 潞化科技 96.46，
  2026-09-03 大元泵业 86.78 / 金富科技 84.77，2026-09-23 依顿电子 75.02

## 六、性能

`zdk_limit_pullback` / `zdk_limit_sideways` 第一版跑 **77 秒**，慢在涨停形态判定用了
lookback(20) 个 `when` 分支 ×4 组 ≈ 80 个嵌套窗口表达式。改成分组累加 + 前向填充的
常数窗口算子后降到 **2 秒**（38 倍），输出逐只完全一致（已用优化的基线比对过）。

其余 20 个策略本来就 1~2 秒级，`zdk_breakout` 2.2 秒是最慢的（它有 `new_high20` 这类
滚动最高价计算）。

两条特殊策略：

| 策略 | 耗时 | 慢在哪 |
| --- | --- | --- |
| `zdk_yaoxin` | 2.6~11.0s | 连板梯队要按 (symbol, 连板段分组) 做 cum_sum，再按日聚合全市场情绪，还要按 (symbol, 节点日) 回查收盘价 |
| `zdk_framework_semi_med` | 2.0~3.8s | 板块等权指数要在 group_by(board, date) 上算 MACD/EXPMA，再加一次 join 回个股 |

`zdk_yaoxin` 的 2.6~11.0s 是两条路径的差异：全量回归里它跑 2.6 秒，单独用 `engine.run()`
跑要 11 秒——后者每次都重新 `build_strategy_context` 载全量历史帧并跑一遍指标管道，
那部分开销不算在 `filter_history` 头上。策略自身（`filter_history` 内）约 2~3 秒，
对「一天跑一次」的选股场景可接受。若后续需要压缩，热点在 `_yaoxin_columns()` 里
`forward_fill().over("symbol")` 那一步。

## 七、怎么改参数

每个策略的 `META["params"]` 里暴露了这些可调项，在客户端策略详情页能直接改：

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `candidate_pool` | 技术面确认前保留多少只候选 | 300~800 |
| `exclude_limit` | 是否排除已涨停的票（|涨幅|≥9.5%） | 多数 true，游资族 false |
| `min_price` / `max_price` | 股价区间 | 3 / 300 |
| `min_turnover` | 最小成交额（亿元） | 1.0 |
| `min_floatcap` | 最小流通市值（亿元） | 30 |
| `exclude_st` | 是否排除 ST | true |

改完不用重启 TSP，点「重新加载策略」即可。

## 八、怎么验证移植是否正确

```powershell
# 1. 加载检查（不跑策略，只验证文件合法）
cd G:\tick_stock_panel\backend
.\.venv\Scripts\python.exe G:\tick_stock_panel\_probe\load_test.py

# 2. 离线跑全部策略（直接调引擎，不经过 HTTP）
.\.venv\Scripts\python.exe G:\tick_stock_panel\_probe\run_zdk.py

# 3. 连真实客户端验证（TSP 需在运行）
.\.venv\Scripts\python.exe G:\tick_stock_panel\_probe\api_verify.py
```

针对两条特殊策略的专项脚本：

| 脚本 | 作用 |
| --- | --- |
| `_probe\load_test_new.py` | 只加载 24 个策略，打印新增两条的 lookback / required_features / params / basic_filter |
| `_probe\run_zdk_new.py` | 跑新增两条，并对 `yaoxin` 做逐门槛拆解（证明 0 结果是行情而非 bug） |
| `_probe\p09_framework.py` | 体检 framework 的中间列（板块多头数、amt3、cum5、KDJ）与九条限制逐条剩余数 |
| `_probe\p10_yaoxin.py` | 回放 yaoxin 的情绪阶段时序与全区间命中票 |
| `_probe\p35_lookahead_mine.py` | **未来函数检验**：对两条特殊策略做「截断帧 vs 全帧」的逐日 symbol 集合比对 |
| `_probe\gen_zdk_boards.py` | 从真实扩展表重新导出 `_zdk_boards.py` / `_zdk_concepts.py`（扩展表更新后重跑） |

未来函数检验的实测结论（`_probe\p35_out.txt`）：把历史帧截断到某日再跑 `filter_history`，
与「全帧跑完再切该日」的结果逐日比对，两条策略的 symbol 集合必须完全一致。

| 策略 | 检验日 | 结果 |
| --- | --- | --- |
| `zdk_yaoxin` | 13 天（6 个出票日 + 末 7 个零票日） | **PASS 13/13** |
| `zdk_framework_semi_med` | 14 天（7 个出票日均匀取点 + 末 7 日） | **PASS 14/14** |

这一条很关键：`framework` 第 1 步的板块等权指数、`yaoxin` 的情绪阶段与节点价，都是
「按日聚合全市场」的横截面量，很容易在实现时把当日之后的行情带进来。上面的检验证明没有。

第 3 个脚本会打印每个策略的结果数和耗时，并落盘 `_probe\api_all_strategies.json`。

注意：PowerShell 控制台会把中文显示成乱码，**这是控制台编码问题，不是数据坏了**。
判断中文正确性要把输出落成 UTF-8 文件再用编辑器看，或者用上面的 python 脚本（内部已经
把 stdout 包成 UTF-8）。

## 九、排查

| 现象 | 原因与处理 |
| --- | --- |
| 策略列表里没有 zdk_* | 文件没放对目录，或引擎没重新加载。确认在 `data\strategies\custom\` 下且是 `.py` |
| 某策略加载失败 | 看客户端日志 `data\logs\desktop.log` 里的 load_errors，通常是 `_zdk_common.py` 没跟着拷 |
| `zdk_yaoxin` / `zdk_framework_semi_med` 加载失败且报 import 错误 | 三条依赖文件要一起拷：`_zdk_common.py` + `_zdk_boards.py` + `_zdk_concepts.py` |
| `zdk_framework_semi_med` 选出的票都是快照里没有行业的新股 | 正常，无板块归属的票会被直接淘汰；扩展表更新后重跑 `_probe\gen_zdk_boards.py` 刷新快照 |
| `zdk_yaoxin` 天天 0 结果 | 先看情绪阶段：`_probe\p10_yaoxin.py` 会打印每日 `stage`。`decline`/`ice`/`main_up` 都不放行，只有 `trial`（低位试错）和 `high_chaos`（高位震荡）才出票 |
| 结果数总是等于 `limit` | 说明候选充足，正常；想看更多就调大 `META["limit"]` |
| 结果数远少于预期 | 先看是不是行情原因（同上面三个 0 结果的策略），再检查 `candidate_pool` 是否太小 |
| 分数都在 60~100 之间 | 正常，这是「百分位排名 + 加分」的量级，不是概率。两条特殊策略例外：`yaoxin` 是 50 起步的加法分（上限 92），`framework` 是 `cum5*0.6+amt3*0.4` 的原始分（无上界） |
