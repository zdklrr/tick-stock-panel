# TSP 面板 — NAS 部署说明

> 部署时间：2026-11-11 · 版本 **v0.3.48**（NAS 两实例）；本机 exe 已到 **v0.3.51**（实盘功能只在本机，NAS 不升级）
>
> **v0.3.51 说明（2026-11-12）｜同花顺拷贝保护过验证码，仍只影响本地 exe，NAS 不动**
>
> 0.3.50 修好前台争用后，读持仓/委托/成交仍失败（资金能读）。根因是同花顺对**表格拷贝**有保护：
> 一发 `^A^C`（或 `WM_COMMAND 0xE122`）就弹 `#32770` 验证码框，剪贴板里是诱饵串。
> 修法三件套：①用 Windows 自带 OCR（`winsdk`/WinRT）替代未安装的 tesseract
> ②必须用 `EnumWindows` 找到**真实**的 `#32770` 对话框 hwnd（`top_window()` 拿到的不是它，这是前几轮全失败的首因）
> ③填框必须 `AttachThreadInput`（`SetFocus` 是线程局部的；实测不 attach 5/5 失败、attach 后 6/6 通过）。
> 详见 `使用说明-TSP客户端.md` 的 v0.3.51 节。
>
> **对本机 exe 的新增要求**：GUI 自动化必须有**活动桌面** —— 锁屏 / RDP 断开 / 切换用户时读数据全失败，
> 提示「当前没有可用的桌面会话…请解锁屏幕后重试」。另 `tsp.spec` 需 `collect_submodules("winsdk")`。
>
> | 位置 | 版本 | 本次动作 |
> |---|---|---|
> | 本机 exe（`%LOCALAPPDATA%\Programs\TSP`） | **0.3.51** | 静默覆盖安装（装区 `data/` 保留） |
> | NAS 923 `http://<NAS_LAN_IP>:923` | 0.3.48 | 不动 |
> | NAS 1923 `http://<NAS_LAN_IP>:1923` | 0.3.48 | 不动 |
>
> **v0.3.50 说明（2026-11-12）｜只修本地 exe 的实盘页，NAS 仍 0.3.48 不动**
>
> 0.3.50 修的是「实盘页读取资金 100% 报 `SetForegroundWindow` 失败」——根因是 TSP 自己的
> WebView2 窗口占着前台，Windows 不许非前台进程抢焦点，而 easytrader 切页要用
> `SetForegroundWindow`。修法是 `AttachThreadInput` 附加到前台线程再抢焦点，并同时 patch
> 三处（`pywinauto.win32functions` / `easytrader.utils.win_gui` / `HwndWrapper.set_focus`）。
> 详见 `使用说明-TSP客户端.md` 的 v0.3.50 节。
>
> **这是纯本机 GUI 层的修复，与 NAS 无关**：NAS 两个实例（923 / 1923）保持 **0.3.48** 不动，
> 无需任何升级或重建动作；923→1923 每天 16:30 的行情同步照常。
>
> | 位置 | 版本 | 本次动作 |
> |---|---|---|
> | 本机 exe（`%LOCALAPPDATA%\Programs\TSP`） | **0.3.50** | 静默覆盖安装（装区 `data/` 保留） |
> | NAS 923 `http://<NAS_LAN_IP>:923` | 0.3.48 | 不动 |
> | NAS 1923 `http://<NAS_LAN_IP>:1923` | 0.3.48 | 不动 |
>
> **v0.3.49 说明（2026-11-12）｜实盘功能只在本地 exe，NAS 两个实例不动**
>
> 0.3.49 新增的「实盘交易」页是用 easytrader 驱动**本机同花顺 Windows 客户端**（GUI 自动化），
> NAS 是 Linux 容器、没有桌面环境，**跑不了这个功能**。因此：
>
> | 位置 | 版本 | 说明 |
> |---|---|---|
> | 本机 exe（`%LOCALAPPDATA%\Programs\TSP`） | **0.3.49** | 新增「实盘交易」页（`/live`） |
> | NAS 923 `http://<NAS_LAN_IP>:923` | 0.3.48 | 保持不动，无实盘入口 |
> | NAS 1923 `http://<NAS_LAN_IP>:1923` | 0.3.48 | 保持不动，无实盘入口 |
>
> NAS 侧无需任何升级动作；923→1923 的每天 16:30 数据同步照常运行（同步的是行情 parquet，与版本无关）。
> 实盘配置存在本机 `data/user_data/extensions/live_trader/`（`config.json`/`state.json`/`live_orders.jsonl`），
> 不在 NAS 上、也不会被同步。
>
> **v0.3.48 统一升级（2026-11-11 20:00）｜923 从 0.3.47 升到 0.3.48，三处版本号一致**
>
> 用户要求（逐字）：「那现在是不是可以把 923 部署的和 1923 一样了，包括本地的 exe 也弄成一样」、
> 「如果能统一，那么版本号应该也一样」、「两个都先留着，只升级不删」、「「建议上限 = 硬闸门、超了就一分不买」这套逻辑」。
>
> | 项目 | 923 生产实例 | 1923 并行实例 |
> |---|---|---|
> | 访问地址 | `http://<NAS_LAN_IP>:923` | `http://<NAS_LAN_IP>:1923` |
> | 版本 | **0.3.48**（原 0.3.47） | **0.3.48** |
> | 源码目录 | `/volume1/docker/TSP/src/tick-stock-panel-main` | `/volume1/docker/TSP1923/src/tick-stock-panel-main` |
> | compose 目录 | `/volume1/docker/TSP` | `/volume1/docker/TSP1923` |
> | 容器名 | `TickFlow_Stock_Panel` | `TSP1923_Panel` |
> | 镜像 | `tsp-nas:0.3.48`（旧 `0.3.47` 已清） | `tsp1923:0.3.48`（本次未动） |
> | 宿主端口 → 容器内 | `923 → 3018` | `1923 → 3018` |
> | data 目录 | `/volume1/docker/TSP/data` | `/volume1/docker/TSP1923/data` |
> | compose 项目名 | `tsp`（由目录推导） | `-p tsp1923`（显式指定） |
> | Web 密码 | <WEB_PASSWORD> | <WEB_PASSWORD> |
>
> **升级步骤（可复现）**：
> 1. `python G:\TSP\_deploy\upload_0348.py` —— 把 0.3.48 全树打成 tar.gz（1372 源文件），
>    经 `sudo -S tee` 二进制写入 `/tmp/tsp0348_src.tar.gz` 并 md5 校验，再 `tar -xzf` 就地覆盖
>    923 构建上下文。**只碰 `/volume1/docker/TSP`，绝不碰 `TSP1923`**；脚本内置守卫：上传前记录
>    `data` 文件数与 `account.json` 数，上传后必须一致（实测 2155/21 前后相同，`bad=0`）。
> 2. `python G:\TSP\_deploy\rebuild_0348.py` —— 只 `sed` 改 923 compose 的 image tag，
>    然后把「build → kill/rm → compose up -d → 轮询 CONTAINER_UP → 清理旧 `tsp-nas:*`」整段
>    用 `setsid nohup` 脱离 SSH 执行（J1900 上 recreate 可能超过 SSH 超时），日志轮询 `SCRIPT_DONE`。
>    **清理镜像只匹配 `^tsp-nas:` 且排除 0.3.48，绝不碰 `tsp1923:*`，也不跑全局 `image prune -f`**。
>    实测 build 190s、`UP_RC=0`、`CONTAINER_UP`、`tsp-nas:0.3.47` 被 Untagged+Deleted。
> 3. 冷启动轮询 `/health`（J1900 约 30~40s）→ `version=0.3.48 / mode=free`。
> 4. `python G:\TSP\_deploy\verify_0348_unified.py` → **TOTAL FAILS: 0 / RESULT: PASS**（19 项）。
>
> **验收要点**：923 跑 `tsp-nas:0.3.48` 且 Up；1923 `TSP1923_Panel/tsp1923:0.3.48` Up 4 hours **未被影响**；
> `data` 卷 **21 个 `account.json`** + `auth.json`(15838→15909 B，容器自己写的) 全在；镜像内
> `paper_plans.SUGGESTED_CAP_ENABLED=True` 且实测 `suggested_exposure_pct=36.2`；
> 21 个账户 `compare` 正常带 `day_pnl/day_pnl_pct`；24 个 zdk 策略 `load_errors=0`；
> 风险雷达 7 指数 `as_of=2026-11-11`；择时 `risk_off/exposure=0.35`；`advice=36.2`；
> 计划 4 条名称 4/4；账户总览 `price_source=live`。
>
> **新功能｜建议上限 = 硬闸门（超了就一分不买）**：每笔自动买入前算「这笔成交后 持仓市值 ÷ 总资产」，
> 超过今日上限就**这一笔不买**（不缩量、不拒单报错、不清老仓），计划留 `pending` 等下轮，
> 原因写进 `last_error`：「今日上限 36.2% (择时×情绪×外围): 买入后仓位将超限, 本次不买入;
> 已有持仓不受影响, 降仓由计划触发」。只拦「自动跟单 + 计划驱动」两条自动买入；手动下单不受限；
> **卖出永远不受择时/风险影响**（铁律不变）。上限 = `clamp(择时×情绪×外围, 0.15, 1.20)×100`，
> 用 `trend_timing(_heal=False)` 只读本地 + 60s 缓存，**买入热路径绝不联网自愈**。
> 总开关 `backend/app/strategy/paper_plans.py` 的 `SUGGESTED_CAP_ENABLED`。
>
> **回滚点**：fork `zdklrr/tick-stock-panel` main = `9569e89a4b29ad3bd879701650d4bb4a18c34aeb`，
> tag `tsp-custom-v0.3.48`（上一版 `tsp-custom-v0.3.47` 保留）。想退回 0.3.47：
> 本地源码用 `G:\tick_stock_panel`（干净 0.3.47），NAS 则 `sed` compose tag 回 `tsp-nas:0.3.47` 后 `compose up -d`
> （但 0.3.47 镜像已在本次清理中删除，需重新 build）。
>
> **提醒**：0.3.48 的上游 `kline_sync.py` 是大重构（1869→1663 行），923 是写入端，
> **升级后第一个盘后数据管道要盯 `docker logs`**；另 923 升级后同样带上 1923 的 `000300.SH`
> 10 天窗口已知限制（见下文专节）。
>
> **v0.3.48 新增｜完整移植上游 0.3.34 + 独立 1923 部署**：
>
> 用户要求「重新复制到 `G:\TSP`，在这个文件夹里修改，部署到 NAS 用端口 1923，选第二项：完整 rebase 再叠加定制」，
> 并明确「不影响改过的功能可以直接移植、会影响的列出来让我选择」。本版是**在全新工作区 `G:\TSP` 里**做的，
> 旧目录 `G:\tick_stock_panel` 仍是干净的 0.3.47，两个 NAS 实例并行运行、互不干扰。
>
> | 项目 | 923 生产实例 | 1923 并行实例 |
> |---|---|---|
> | 访问地址 | `http://<NAS_LAN_IP>:923` | `http://<NAS_LAN_IP>:1923` |
> | 版本 | **0.3.48**（2026-11-11 由 0.3.47 升级，见文首统一升级节） | **0.3.48** |
> | 源码目录 | `/volume1/docker/TSP/src/tick-stock-panel-main` | `/volume1/docker/TSP1923/src/tick-stock-panel-main` |
> | compose 目录 | `/volume1/docker/TSP` | `/volume1/docker/TSP1923` |
> | 容器名 | `TickFlow_Stock_Panel` | `TSP1923_Panel` |
> | 镜像 | `tsp-nas:0.3.48` | `tsp1923:0.3.48` |
> | 宿主端口 → 容器内 | `923 → 3018` | `1923 → 3018` |
> | data 目录 | `/volume1/docker/TSP/data` | `/volume1/docker/TSP1923/data` |
> | compose 项目名 | `tsp`（由目录推导） | `-p tsp1923`（显式指定） |
> | Web 密码 | <WEB_PASSWORD> | <WEB_PASSWORD> |
>
> 1. **完整 rebase**：上游 `shy3130/tick-stock-panel` 自 fork 基线（0.3.26）之后的 **53 个提交 / 397 文件变化**
>    全部合入（部署稳定性修复、K 线分钟补缺、图表引擎统一到 lightweight-charts 5.2.1、
>    策略池分组/归档/备份恢复、自选股图表、回测净值回撤图等），**同时保留我们 0.3.27~0.3.47 的全部定制**
>    （zdk 22 策略、腾讯/新浪免费源、风险雷达/盘前简报/大盘择时四卡、模拟盘自动仓位与择时缩放、
>    今日盈亏、名称三层兜底、策略池删除、策略→交易计划、手机响应式等）。
> 2. **上游「针对老版本已有功能的优化升级」已全部吸收、0 遗漏**（`_verify\verify_all_upstream_changes.py`
>    全树扫描：upstream 变化 393 文件 → UPSTREAM_OK=363 / MERGED=30 / 丢失=0；28 个双方都改的文件
>    `merged=28 / upstream_lost=0 / ours_lost=0`，21 个上游修复标记全在）。代表性优化：Windows 白屏、
>    覆盖升级配置丢失、Docker 路径错位丢配置、无控制台连接探测、K 线 `quote_ts` 列兼容(#537)、
>    历史分钟 K 早盘补缺、自选添加自动加载日K/分时、分时昨收零轴对称、策略卡片参数误判、分钟 bar 归一等。
>    结论：无需再补改（改了反而有回归风险），我们的定制功能一个都没动。
> 2b. **11 个双方都改的文件逐个手工三方合并**（以上游为底重放我们的定制），不是整块保留旧版；
>    `StrategyPoolDialog` 以上游 495 行重写为底、**补回我们 0.3.28 的「删除策略文件」功能**（上游重写丢掉了它）。
>    详见 `G:\TSP\使用说明-TSP客户端.md` 的 v0.3.48 节。
> 3. **两条移植路径都做了真实编译裁决**：按主题挑 242 文件会切断上游内部一致性（不可编译，已完整还原）；
>    最终用 git 全量三方合并树 + 逐个修。本地 `tsc` 0 错、`vitest 849 passed`、`pytest 234 passed`、`vite build` 成功、
>    全树 0 冲突标记、10/10 定制功能标记存活。
> 4. **部署脚本**（全在 `G:\TSP\_deploy\`，必须用 dsh python 跑 paramiko）：
>    `upload_1923.py`（tar 白名单流式上传 + md5 校验 + 18 项抽查）、
>    `rebuild_1923.py`（只 `docker compose -p tsp1923`、清镜像只匹配 `^tsp1923:`、禁 image prune、绝不碰 923）、
>    `verify_1923.py`（30+ 项隔离验收）、`nas_1923_baseline.py`（部署前 923 基线）。
> 5. **首轮构建失败已修**：`upload_1923.py` 的裸名 `data` 排除规则误剪真实源码 `frontend/src/data` 与
>    `frontend/src/components/data`(27 模块)，导致 `pnpm build` 报 `TS2307`。改为只锚定根级 `data`/`backend/data`，
>    `.dockerignore` 同步改 `/data` + `/backend/data`；重传 source files 1343→**1371**，二次 `BUILD_RC=0`。
> 6. **验收 `verify_1923.py` → FAILS=0**：1923 `/health`=0.3.48、`/api/data/version`=v0.3.48、
>    `/api/risk/radar` 可用；1923 容器/镜像/端口/挂载全部隔离，923 与 1923 data 目录 inode 不同、marker 不泄漏。
>    （注：此条记录的是**1923 首部署时**的 923 基线——当时 923 是 0.3.47。**2026-11-11 20:00 起 923 也已升到 0.3.48**，
>    见文首「v0.3.48 统一升级」节；升级后 923 容器与镜像均为 0.3.48，1923 未受任何影响。）
> 7. **`mode=none` 已解决（1923 共用 923 的数据源与日K）**：见下方「**v0.3.48 补充｜923 → 1923 数据同步**」一节。
>    1923 原本是全新空 data 目录、没配数据源，`/health` 的 `mode=none`，打开个股详情会报「日K数据源获取失败」。
>    现已一次性共享 923 的 `capabilities.json` / `secrets.json` / `preferences.json` 让 1923 变 `mode=free`，
>    并把 923 已下载的日K/分钟K/维表/复权/fuyao 缓存单向同步过来，且每天 16:30 自动同步。
>    `paper/compare` 仍是 1923 自己的空账户（模拟盘/密码哈希刻意保持独立，不共享）。
>
> **v0.3.48 补充｜923 → 1923 数据同步（用户：能不能两边共用一个数据源和下载好的日K）**
>
> 用户选「**独立 + 每天自动同步**」：两个实例各保留独立 `data/` 目录（不整目录共享，避免两容器同时写同一批
> parquet / `.lock` 损坏数据、模拟盘账户混杂），由 923 单向把「行情数据」同步给 1923，923 全程只读、绝不重启。
>
> | 项 | 内容 |
> |---|---|
> | 一次性配置共享 | 宿主侧 `sudo cp` 把 923 的 `data/capabilities.json`、`data/user_data/secrets.json`、`data/user_data/preferences.json` 复制到 1923（密钥不离 NAS），`secrets.json` chmod 600；复制后 1923 `/health` 从 `mode=none` 变 **`mode=free`** |
> | 每日单向同步 | `/etc/cron.d/tsp1923-data-sync` 每天 **16:30（北京时间，避开 923 15:35 盘后管道）** 以 root 跑 `/volume1/docker/TSP1923/sync_from_923.sh` |
> | 同步范围（取并集） | 10 个 `kline_*`/`adj_factor*` 分区目录 + `cache/fuyao`（181MB 全市场日K dump）：`rsync -a --ignore-existing --include='*/' --include='*.parquet' --exclude='*'`，不覆盖 1923 自建分区、不加 `--delete` |
> | 维表（923 权威覆盖） | `instruments`/`instruments_etf`/`instruments_index`/`instruments_ext` **不加 `--ignore-existing`** —— 否则 1923 会一直保留自己那份不完整维表（实测 68KB 缺深市名称，923 是 192KB 完整表），表现为「1923 股票只有代码没有名字」|
> | 绝不同步 | `user_data/auth.json`（各自的 Web 密码哈希，0600）、`paper/`、`job_store/`、`regime_history/`、`mainline_history/`、`upgrade_guard/`、`strategies/`、`logs/`、任何 `.lock`/`.tmp`/`.bak` |
> | 何时重启 1923 | 仅当「有新增 parquet 文件」或「维表内容变化」时 `docker restart TSP1923_Panel`（重建 Polars 内存缓存 / DuckDB 视图 / `get_name_map` 缓存）；稳态（无新文件）跳过重启。脚本内 `flock` 防重入、日志封顶 2MB |
> | 关键脚本 | `G:\TSP\_deploy\sync_from_923.sh`（同步本体，也部署到 NAS）、`setup_data_sync.py`（`--apply` 一次性落地 + `--verify` 18 项验收）、`verify_1923.py`（实例隔离验收） |
>
> - **验收 `setup_data_sync.py --verify` → 18/18 PASS**（记录于 923 仍是 0.3.47 时）：923 容器 ID / StartedAt / Image / Status 全部未变、
>   `/health` 仍 0.3.47；1923 `auth.json` 未变（登录 <WEB_PASSWORD> 仍可用）、`mode=free`、`kline_daily` 249 分区、
>   `/api/kline/daily?symbol=600000.SH&days=10` 返回「浦发银行 8 行」、cron 已注册、sync.log 有完整 run。
>   **2026-11-11 20:00 之后 923 已是 0.3.48**，同步脚本本身不受版本影响（它只 rsync 宿主上的 parquet 文件，与容器版本无关）；
>   923 重建只换镜像、data 卷原位不动，故 cron 无需重装。
> - **首次全量同步**：`new parquet files = 1017`（约 380MB 行情数据），923 → 1923 拉平；随后一次 v2 修正把维表
>   改为 923 权威覆盖，重跑后 8 只抽样股票（含深市 `000001.SZ`/`300007.SZ`/`002088.SZ`/`300473.SZ`）**名称与
>   行数全部与 923 一致**。
> - **已知限制（与数据共享无关）**：指数 `000300.SH` 走股票日K端点、10 天窗口时，923 返回 10 行而 1923 返回 0 行；
>   两容器 `kline_index_daily` parquet 逐字节相同、`days=30/120/指定区间` 均完全一致（各 15/73/17 行），
>   这是 0.3.48 与 0.3.47 在「空结果触发 `daily_bootstrap.ensure_history`」路径上的运行时差异，
>   `000001.SH`/`399001.SZ` 等其它指数均正常。指数行情本身已同步、不影响大盘择时/情绪周期/风险雷达。
> - 备份/回滚：`/volume1/docker/TSP1923/data` 是 1923 唯一数据源；同步只增不删（`--ignore-existing`），
>   随时可删掉 `/etc/cron.d/tsp1923-data-sync` 停止自动同步，923 侧从未被写入任何文件。
>
> **v0.3.48 补充｜923 策略库复制到 1923（用户：把 923 端口的策略也复制过去）**
>
> - 923 的 `data/strategies/custom/` 有 **27 个 .py**（24 个 zdk 策略 + 3 个 `_zdk_*` helper）+ 2 个 README + `.seeded_v1`；
>   1923 原本只有 `.seeded_v1`、`/api/strategies` 返回 `{"strategies":[]}`。
> - 用 `G:\TSP\_deploy\copy_strategies_to_1923.py`（宿主侧 tar 流式复制，`--exclude=__pycache__`，**923 全程只读**）
>   把 27 个 .py 逐字节复制到 1923，再用 `finalize_strategies_1923.py` 从 Windows 端 HTTP 登录调
>   `POST /api/strategies/reload` 热加载 → **`{"ok":true,"count":24}`、`load_errors: []`**，
>   24 个 id 全部可用（`zdk_breakout` … `zdk_yaoxin`），1923 `/health` 仍 0.3.48（923 当时仍 0.3.47，
>   **2026-11-11 20:00 起也已升到 0.3.48**，且 923 的 27 个策略文件随源码树一起重传，仍在 `/volume1/docker/TSP/data/strategies/custom/`）。
> - **没有复制「策略池勾选状态」**：0.3.47（923）的策略池选择存在浏览器 localStorage（`storage.ts` 的
>   `strategy-pool`），服务端没有副本；0.3.48（1923）改成服务端 `data/user_data/strategy_pool.json` v2。
>   两者存储模型不同，需要群策略的话在 1923 界面里重新勾选即可（或按 24 个 id 生成 v2 池文件）。
>   注：策略**定义文件**不在每天 cron 同步范围内（上面「绝不同步」清单里的 `strategies/`），
>   本次是一次性复制；以后 923 新增策略要再跑一次复制脚本。
>
> **v0.3.48 补充｜模拟盘「当前仓位 / 计划仓位 / 今日上限」口径说明（用户：持仓比计划和上限还高）**
>
> 三个数字是**不同口径**、不是同一个数，出现「当前仓位 > 今日上限」属正常，不是 bug：
>
> | 数字 | 怎么来的 | 会不会变 |
> |---|---|---|
> | **当前仓位** | `实时持仓市值 ÷ 总资产`（`PlanPanel.tsx` `positionPct = market_value/total`） | 随行情实时变，**股价涨了就变大**（含浮盈） |
> | **计划仓位** | 建计划那一刻冻结的各计划 `pos_pct` 之和（`planPosPct = Σ pos_pct`） | 冻结值，**不随行情重算** |
> | **今日上限** | 此刻 `/api/risk/advice` 的 `suggested_exposure_pct` = `大盘择时 × 情绪周期 × 外围风险`（`market_timing.synthesize()`，夹 `[0.15,1.20]`），再受 `plan_config.max_total_exposure_pct` 约束 | **盘中随时跳**，情绪周期阶段/外围分数一变就变 |
>
> - **铁律：卖出永不受择时/风险系数影响**。「今日上限」是对**今天新买入**的前瞻软建议，
>   系统绝不会因为盘面转弱自动卖掉已有持仓 → 老仓可以「超标」。
> - 实例（923 `arena_fc343cf626`，2026-11-11）：14:06 截图时 `当前仓位 58.2% / 今日上限 36.0% / 计划仓位 56.8%`；
>   36% = `择时 0.35(risk_off 空头趋势) × 情绪 1.0(lean_strong) × 外围 1.03 ≈ 36%`。
>   14:24 复查时情绪周期升级为 `strong(×1.2)`、外围 1.043 → 上限跳到 **43.8%**。
>   计划 `timing_scale=1.0 / state=lean_strong` 说明这 3 只是**上午盘面偏强时建的仓**，下午大盘转 `risk_off` 后上限才降到 36%，
>   属正常时序差。当前仓位 58% 高的另一原因是三只票涨价（含浮盈），市值口径比建仓时大。
> - 真正想封顶：改 `plan_config.max_total_exposure_pct`（默认 100 = 不生效）。但**它只挡新买单，不会卖老仓**；
>   要让已持仓降到上限以下，只能等计划触发的止损/目标价减半/清仓（卖出路径本身永不打折）。
> - 自动跟单买入的每票金额：`G:\TSP\backend\app\strategy\paper_auto.py` 的 `equal_total` 分支
>   `总仓% = 100×择时×情绪×外围回撤`、`每票% = 总仓% ÷ 缩放后的计划只数上限`，**刻意不用 synthesize()**（避免情绪重复计入）。
>
> **v0.3.47 新增**（修「收起状态看不到今日盈亏 / 交易计划只有代码没名字 / 看不懂撮合异常」）：
> 1. **账户对比表的「最新日」列改名为「今日盈亏」并接到正确字段** —— 原来该列取的是 `day_change_pct`
>    （最近两个**定版净值**的涨跌），当天未结算或净值不足 2 天时恒为 `None`，前端只能显示「—」。
>    现排序键改 `day_pnl_pct`、取值 `day_pnl_pct ?? day_change_pct`（有昨收用今日盈亏，无昨收退回净值涨跌并灰字标注）。
>    后端 `day_pnl` / `day_pnl_pct` 在 0.3.46 已算好，本次只把前端接到正确字段。
> 2. **交易计划里只有代码没有股票名** —— 自动跟单成交后 `ensure_plan_for_fill(...)` 补建计划时
>    **没有把 name 传下去**，NAS 上 28 条 `source=auto` 计划的 `name` 全是 `None`。两层修：
>    ①补建时把订单自带 name 传进计划；②`/api/paper/plans` 对**存量**计划就地回填 —— 与持仓同一套三步兜底
>    （行内 name → `get_name_map` 维表 → `lookup_names_online` 腾讯快照），纯展示、不改任何数值字段。
> 3. **「撮合异常」四个字看不懂** —— 前端原先把 `last_error` 真原因藏在 `title` 悬停里，正文只有「撮合异常」
>    且用红色（暗示系统故障）。实际 NAS 实测 4 条 `last_error` **全是正常业务状态**：
>    `买入单未成交 (拒单或过期)`（涨停排队没成交）、`触及目标1 11.48 减半, 但当前无 T+1 可卖数量 (可卖 0)`
>    （目标价到了但当天买入的不能卖）。现**直接显示原文**（截断 + 悬停看全文），颜色改 `text-warning`，
>    悬停补一句「这是撮合过程中的状态说明, 不是系统异常」。
> 4. **卖出路径与撮合判定逻辑零改动**（本次只改展示层字段接线）。
> 5. **验证**：后端 `pytest` **166 passed**；前端 `tsc -b` 0 错误、`vitest` **73 files / 541 passed**；
>    本地 `TSP-Setup-0.3.47.exe` 135,787,902 字节，安装后 `/health`=0.3.47、装区 data 388.2 MB 保留。
>
> **v0.3.46 新增**（修「模拟盘持仓股票名字没了 / 只有总盈亏没有今日盈亏 / 非交易时段浮盈恒为 0」）：
> 1. **持仓只剩代码、没有股票名称** —— 根因是**下单不落 name**，且名称唯一来源 `data/instruments/instruments.parquet`
>    在 NAS 上被降级行情源冲成只剩沪市 **2823 行**（深市整行缺失），20 只持仓只有 4 只能查到名字。三层修：
>    ①`create_order(name=)` → fill → `positions.json` **逐级落 name**；②维表加**缩表护栏**（新源行数少于现有就拒绝写入）
>    + 按 symbol 反连接**补回整行缺失标的** + name 纳入「不得用空值覆盖」；③展示层兜底 `lookup_names_online()`
>    直接问腾讯实时快照（10 分钟缓存、失败静默），**完全不依赖那张坏维表**。
> 2. **新增今日盈亏** —— 原来只有「现价 − 成本」的累计浮盈。现每只持仓与账户级都出 `day_pnl` / `day_pnl_pct` /
>    `prev_close` / `price_date`；账户级优先用「当前总资产 − 上一交易日定版净值」（含当日已实现盈亏）。
> 3. **非交易时段浮盈不再恒为 0** —— `overview` 新增 `valuation="daily"` 口径，用最近一根日线收盘价估值
>    （离线可算、30 秒缓存、21 账户共用一份）。**默认仍是 `"cost"`**，因为建计划/结算拿 `overview()["total"]` 当权益。
> 4. **盘中实时分支不再把昨收抹成 null** —— 首轮部署后 NAS 实测 2/2 持仓 `day_pnl` 全空；现实时分支同样回日线取昨收，
>    且**日线最新一根若是今天（盘中未定型 bar）则昨收退到再前一根**。
> 5. **`/api/kline/instruments/names` 也走在线兜底** —— 首轮实测仍只回 1/5，因该接口只查维表；现已补上。
> 6. 前端：账户总览卡 4 张 → **5 张**（新增「今日盈亏」）；持仓表拆分「累计盈亏」/「今日盈亏」两列（悬停显示昨收）；
>    名称列改为 `持仓 name → 维表名称`；非交易时段注明「按最近日线收盘价估算（行情日 xxxx-xx-xx）」。
> 7. 验证：pytest **246 passed**（新增盘中昨收 2 例 + 在线名称 2 例）；`tsc -b` 0 错误、vitest **73 files / 541 passed**；
>    NAS 实测 `VERIFY_EXIT=0 / FAILS 0`：21/21 账户 `day_pnl` 有值、`arena_6f094841ef` 持仓名 2/2、`price_source=live`、
>    `instruments/names` 5 只全命中。部署脚本 `_deploy/upload_0346.py`（15 文件）+ `_deploy/rebuild_0346.py`
>    （setsid 脱离会话执行 kill → rm 残骸 → up -d → 清旧镜像），镜像 `tsp-nas:0.3.46`。
>
> **v0.3.45 新增**（修「竞价结束后看盘界面情绪周期 / 大盘择时还是昨天」的问题）：
> 1. **免费回补只补缺的交易日** —— 原实现每次把腾讯返回的 320 根日K 全量 merge-upsert
>    （321 个日分区逐个读写），NAS 上一次要 4 分钟；现在只写本地缺失的那几天（新装时每轮
>    最多 20 个分区），常态一次只写 1 个分区。
> 2. **不再刷屏、不再拖死接口** —— 原来的「stale → free backfill」日志打在取锁之前、且用进
>    函数时的旧日期判断，并发请求每 1~2 秒刷一条并全堆在锁上，导致 `/api/risk/timing` 与
>    `/api/risk/advice` 超时 >120 秒；现改为按标的占位（`_inflight`）、拿到占位后重读最新
>    日期再决定是否拉取，单轮网络预算 6 秒、返回 `busy` / `source-lagging` / `cooling`。
> 3. **落后时重试间隔从 30 分钟缩到 2 分钟** —— 09:25 竞价结束时数据源常还没出当天那根，
>    原先要等 30 分钟冷却；现在 `_PENDING_RETRY=120` 秒，且连续不前进时按 ×2 退避（封顶 15 分钟）。
> 4. **情绪周期 / 择时的后台补算看盘口时段** —— 工作日 09:15–15:05 每 3 分钟复查一次，
>    其余时段 30 分钟一次（`market_time.data_refresh_interval_seconds`）。
> 5. **请求路径不再同步等网络** —— `trend_timing` 的同步自愈每标的只写 2 个分区
>    （`_SYNC_MAX_WRITE_ROWS`），剩下的缺口交给后台线程。
> 6. 前端看板「大盘择时」「情绪周期」两张卡的轮询从 5 分钟缩到 2 分钟，与后端节奏对齐。
> 7. 验证：pytest **150 passed**（新增 `backend/tests/test_timing_freshness.py` 6 例 +
>    `test_index_daily_free_backfill.py` 6 例）；NAS 实测 `/api/risk/timing` → `as_of=2026-11-06`
>    秒回（修复前超时 >120s）、`/api/regime/latest` → 2026-11-06。
>
> **v0.3.44 新增**（修 0.3.43 初版跟单仓位的三个问题，功能口径不变）：
> 1. **缩放后的只数不再超过你设的上限** —— 原 `round(5×择时1.2×情绪1.2)=7` 只 > 用户上限 5；
>    改为先取整、再夹在 `[1, 上限]`，强市最多就是你设的只数，弱市最少 1 只。
> 2. **第一票不再吃掉全部仓位** —— 原分母用「已持仓+1」，择时中性时第一票按 100% 权益买；
>    改为按「本期计划只数」均分：优先跟单规则 `max_positions`（缩放后）→ 其次计划配置 `max_symbols`
>    （`effective_holdings_limit` 同一口径）→ 都没有才退回「已持仓+1」且每票受 `max_pos_pct`（默认 20%）约束。
> 3. **有额度却一只没买** —— 建议总仓 100% 时末几票加上手续费+滑点超现金 → 被资金预检拒单；
>    买入金额乘 0.995 预留费用余量（新增 `_affordable`）。
> 4. **总仓均分补上风险雷达系数** `radar_adj=clamp(外围×账户回撤, 0.5, 1.5)`，与交易计划同口径
>    （**仍不用 `synthesize()`**）。卖出路径零改动。
> 5. 验证：pytest 合计 **216 passed**（新增 `backend/tests/test_paper_auto_scaling.py` 16 例）；NAS 实测
>    `/health`=0.3.44、21/21 账户规则 `position_mode=equal_total`。
>
> **v0.3.43 新增**：
> 1. **自动跟单「最多持仓只数」随大盘择时缩放** —— 此前跟单规则的 `max_positions` 是硬上限（永远最多买 N 只）。
>    现改为乐观上限 ×（大盘择时系数 × 情绪周期系数）取整、最低保留 1 只；弱市自动缩到 3/2/1 只。
>    改动在 `backend/app/strategy/paper_auto.py` 的 `on_rule_events` 买入分支。
> 2. **新增「仓位分配」开关（默认「总仓均分」）** —— `position_mode` 新字段：
>    `fixed` = 沿用原「每票固定百分比」；`equal_total` = 每票 = 大盘择时 × 情绪周期建议总仓 ÷ 实际持仓只数。
>    建议总仓系数取 `trend_exposure × exposure_multiplier`（**不用 `synthesize()`**，避免与外围风险重复计入）。
>    存量 21 个 arena 账户规则已用 `_deploy/migrate_position_mode_0343.py` 全部迁移为 `equal_total`。
> 3. **持仓表「买入区间」列恒显** —— 此前只在 `entry_mode='zone'` 时显示区间，而自动跟单建的是 `market` 计划
>    → 永远 `--`。现改为只要计划带出 low/high 就显示（前端 `custom/paper/Page.tsx` + `holdingsColumns.test.tsx`）。
> 4. **卖出路径零改动** —— 择时/情绪系数只作用于买入股数与只数上限，卖出（止损/目标1减半/目标2清仓）不受任何打折。
>
> **v0.3.42 新增**：
> 1. **看板风险雷达/盘前简报展开单指数明细** —— 此前只显示「美股/亚太/恒生」三个聚合区域分，
>    用户反馈「亚太原片面，应显示日本、韩国、恒生」。现风险雷达卡每个区域下展开单指数
>    （美股:道琼斯/纳斯达克/标普500；亚太:日经225/首尔综合指数/TWII；恒生:香港恒生指数），
>    盘前简报卡下展开亚太/美股单指数涨跌幅。纯前端改造，后端 `indexes`/`asia.rows`/`us.rows` 本就齐全。
>    NAS 实测风险雷达卡显示「日经225 −1.79% 首尔综合指数 −5.28% 台湾台北指数 — 香港恒生指数 +3.67%」。
>
> **v0.3.41 新增**：
> 1. **盘中就显示当天数据** —— 此前「当天」要到 15:05 收盘后才成立，盘中一律认为昨日即最新，
>    于是上午看板的大盘择时/情绪周期永远是昨天的。现改为 **09:25 竞价结束**起即认当天，
>    并自动从腾讯免费源补当天那根 K。
> 2. **盘中数据标「盘中·未定型」** —— 当天 K 未收盘，均线与四态会跳动；界面给最新值但明确标注。
> 3. **盘前 09:20 主动刷新** —— 择时/情绪周期/风险雷达决定仓位与只数，必须先于开盘就绪。
>    新增 `market_context_refresh.refresh_market_context()` 一次做完三路 + 清缓存，
>    挂到调度器 `premarket@09:20`（mon-fri）。NAS 启动日志可见四个时刻。
> 4. **请求路径不再同步等网络** —— 第一版盘中刷新是同步的，四张卡同时请求导致
>    `/api/risk/timing` 在 NAS 上 **180 秒超时**。现拆成「数据缺失→同步回补」与
>    「盘中未定型→后台线程刷」，配 60 秒节流 + 线程存活检查。
> 5. **并发去重 + 盘中只写当天一行** —— NAS 实测 46 秒内打了 3 次腾讯、每次 merge-upsert
>    321 个日分区。加 `_fetch_lock` 锁内二次判断，盘中只写当天那行。
>
> **v0.3.40 新增**：
> 1. **择时「滞后」改按交易日算** —— 此前 `_stale_days` 用自然日差，交易日收盘后数据停在昨日时
>    自然日差恒为 1，而自愈阈值是 `> 1` → **收盘后整晚不回补**，大盘择时卡一直挂「滞后 1 天」。
>    新增 `_expected_as_of()`（收盘 15:05 后应有当日数据，周末退到上周五），
>    `_is_behind_expected()` 判落后、`_stale_days()` 改数工作日。
>    NAS 实测 `/api/risk/timing` → `as_of=2026-11-04 stale=0`（凌晨 00:52 查看，不虚报滞后）。
> 2. **全新安装也能补上指数日K** —— 原实现在「数据目录不存在」「根数 < 60」两处提前 return，
>    都排在自愈代码之前；已把 `_build_snapshot()` 与自愈拆开，所有降级路径都能走到免费源回补。
> 3. **情绪周期/主线改周期性补算** —— 启动 45s 后补一次，之后每 30 分钟复查缺口
>    （原来只补一次，桌面端连开几天会再次停更）。
>
> **v0.3.39 新增**：
> 1. **大盘择时数据自愈真正生效** —— 门控从「调用方有没有省 data_dir」改为按路径身份判定
>    （必须是正式数据目录且不在系统临时目录下）。此前 HTTP 层一律显式传 `repo.store.data_dir`，
>    生产路径上自愈是死代码，只有手写脚本能救。
> 2. **情绪周期/主线启动补算** —— 启动延迟 45s 起后台线程补算缺口
>    （`compute_regime_incremental` + `compute_mainline_incremental` concept/industry），
>    与盘后管道开关无关，软失败不影响启动。
>    NAS 实测 `/api/regime/latest` → `date=2026-11-04 state=strong score=83 phase=repair`。
>
> **v0.3.38 新增**：
> 1. **看板涨停家数修正（第二层）** —— 假涨停/假跌停只对「当前仍在涨停名单里」的标的扣减
>    （此前 sealed 修正用修复前的旧快照，把 17 只已不在名单的票又扣一次，122−17=105）；
>    NAS 实测 `/api/overview/market` → `limit_up=121 limit_down=11 seal_rate=77.07 fake_up=1`。
>
> **v0.3.37 新增**：
> 1. **看板涨停家数修正（第一层）** —— `instruments` 维表的 `limit_up/limit_down` 是按 11-02 收盘
>    算的陈旧快照（与 11-02 收盘 99.9% 精确匹配）却被标 `as_of=11-04`，导致 88 只真涨停漏判、
>    16 只普通上涨误判；改为反推 implied_prev 识别陈旧快照后回退理论价。
>    看板涨停从 **34 → 121**，封板率 **45.3% → 77.1%**。
>
> **v0.3.36 新增**：
> 1. **情绪周期不再断更（三件套治本）** —— 看护（watchdog）区分「后台管道持锁忙」与「进程僵死」，
>    写锁被长任务持有只记警告不再自杀重启；除权因子连续 3 个分块全失败快速短路（40s→3s）；
>    情绪周期/主线计算前移到指数同步之前。NAS 实测 `/api/regime/latest` → `date=2026-11-04 lean_strong 67`；
> 2. **持仓表新增「买入区间」列** —— 显示活跃计划的 entry_low~entry_high（zone 模式），
>    与止损/目标列构成 zdk 风格完整持仓视图，无计划时显示 `--`。
>
> **v0.3.35 新增**：
> 1. **大盘择时/情绪周期数据自愈** —— 内置腾讯免费日K回补（不依赖 tickflow key），
>    择时数据滞后 >1 交易日自动拉 000300/510300 各 320 根回补并重算；
>    NAS 实测从 `as_of=2026-10-30 stale=5` 恢复为 `as_of=2026-11-04 stale=0`；
> 2. **模拟盘持仓表列序对齐 zdk** —— 代码→数量→现价→成本→盈亏→止损→目标1→目标2→市值→仓位%→可卖→来源；
> 3. **买入成交自动生成交易计划**（来源=自动），持仓表止损/目标列对新成交从此有数据（存量不追溯）；
> 4. **账户列表去掉「对比·xxx」蓝色徽标**（规则仍生效）。
>
> **v0.3.34 新增**：
> 1. **模拟盘持仓表补全交易计划信息** —— 新增 仓位%（占总资产）/ 止损 / 目标1（到价减半）/
>    目标2（到价清仓）/ 来源（策略·手工·自动跟单）五列，与交易计划弹窗口径一致，缺失显示 `--`；
> 2. **提示带账户名** —— 模拟盘成交弹窗开头 `[账户名]`、自动跟单提醒 `账户[xxx] …`，
>    多账户时一眼分清是谁买的；事件本身也新增 `account_name` 字段。
>
> **v0.3.33 新增**：
> 1. **看板四张卡都标明数据日期** —— 盘前简报显示「预判日」、大盘择时/情绪周期/风险雷达显示「数据 YYYY-MM-DD」，
>    一眼看出是今天的还是几天前的；数据陈旧时自动加琥珀色「滞后 N 天」徽标，后端没给日期时显示「日期未知」（不假装是今天）；
> 2. **盘前简报的三个驱动源各自标注日期** —— A50 期货/外汇是今天、美股隔夜是上一交易日、亚太同期是最近交易日，
>    三者本来就不同天，现在分得清；
> 3. **修掉一个会误导人的真问题** —— 新浪美股时间戳是北京时间，凌晨 06:30 那根其实是**昨天**的美股收盘，
>    原来会被标成「今天」；现在正确显示为上一交易日（`IXIC.as_of` 从 `2026-11-04` → `2026-11-03`）。
>
> **v0.3.32 新增**：
> 1. **手机端二级界面不再挤成竖条** —— 策略设置、策略构建器等 9 个弹窗改成窄屏单栏、
>    左侧导航变顶部横向标签，390×844 实测无横向溢出、无竖排文字；
> 2. **「生成交易计划」不再一片灰** —— 算不出的行也会给出买入区间/止损/目标/仓位，
>    并用中文写清原因（例：「计划股数不足一手 (按仓位 12.0% 只能买 0 股), 一手 2196.0 元…」）；
>    弹窗顶部新增「大盘择时」「情绪周期」两个独立徽标（分别显示真实择时系数与情绪倍率）；
> 3. **看板四卡一卡一色** —— 盘前简报(蓝)/大盘择时(琥珀)/情绪周期(红)/风险雷达(绿)，
>    状态色只用于外框、关键数字与等级徽标。
>
> **v0.3.31 新增**：
> 1. **交易计划执行方式三选一** —— `按买入区间`(默认) / `次日开盘价` / `即时市价`，
>    不必再等次日开盘；**卖出逻辑完全不变**（止损/目标仍只由价格触发）；
> 2. **看板新增四张小卡片**：盘前简报 / 大盘择时 / 情绪周期 / 风险雷达，置于情绪雷达上方，
>    与「市场环境」页共用同一组 query key；**大盘择时与情绪周期会缩放模拟盘的持仓只数与仓位**；
> 3. **监控中心降噪开关**：可隐藏「自动跟单」类（默认隐藏）与「成交」类（默认显示），
>    只看策略与模拟盘；同一偏好在监控页、看板监控卡、右下角 toast 一起生效（只过滤显示，照常落盘）；
> 4. **优中选优**：自选持仓数上限 → 按策略评分取前 N → 择时/情绪自动缩放只数与每只仓位，
>    超总额度时等比压缩、不足一手转 skipped；
> 5. **模拟盘账户详情**新增「今日决策」卡与更详细的持仓/计划表（预期赚亏、盈亏比、状态徽标）。
>
> **v0.3.30 新增**：
> 1. **监控中心触发记录说清「是什么触发的」** —— 自动跟单事件自带股票名称、规则名、策略 id、
>    账户名与触发原因，弹窗/语音/留痕/webhook 四条通道一致；**历史记录也会按 symbol 回填名称**
>    （实测 220 条模拟盘旧记录，回填前 0 条有名称，回填后 220/220 有）；
> 2. **模拟盘多选批量操作**（启动 / 停止 / 批量删除）+ 单账户「**重置账户**」，
>    并且「停止」现在是真停止（不撮合、不写净值、不推进计划）；
> 3. **市场环境 → 情绪周期新增「大盘择时」卡**（沪深300 vs MA20/MA60 四态）与
>    **综合建议仓位**（择时 × 情绪 × 外围三路相乘，分列展示谁在压仓位）；
> 4. **策略选股 → 交易计划**：结果表勾选标的 → 弹窗预览买入区间/止损/目标/仓位/股数/盈亏 →
>    勾选后生成，买不买由你决定（一次最多 100 条）。
>
> **v0.3.29 新增**：
> 1. **风险雷达**（美股 / 日韩台 / 恒生，7 个指数）与 **盘前简报**（A50 + 美股 + 亚太 → 高开/平开/低开概率），
>    都在 **情绪周期** 页签；风险等级变化会主动弹提示、进监控中心、发系统通知/飞书；
> 2. 模拟盘仓位改为**策略选出的仓位 × 大盘系数 × 外围风险系数 × 账户回撤系数**自动决定，
>    「最多持有几只 / 总仓位上限」两个人为闸门**默认不限**；
> 3. 修复被频控挤掉的风险提示会永久丢失（下一轮补播）。
>
> **v0.3.28 新增**：
> 1. **策略池可删除策略文件**（垃圾桶按钮 + 二次确认）；
> 2. zdk 策略从 22 个增到 **24 个**（新增「要信早信」「心法框架·动态多头板块」）；
> 3. 模拟盘**三档订单类型图文详解** + **撮合口径默认开启**（涨停买不进、跌停卖不出，排队次日重试）；
> 4. 模拟盘新增「**交易计划**」：买入区间、止损、目标1/2、仓位占比、成本、盈亏金额，
>    价格进区间自动买、到点位自动卖，**仓位与止损随大盘情绪自动缩放**。
>
> **v0.3.27 新增**：腾讯行情（免费）、新浪行情（免费）两个数据源插件，
> 补齐了 **实时行情 / 五档盘口 / 分钟K / 全量分钟** 四项能力，不再依赖 TickFlow 付费档位。

## 一、访问方式

| 项 | 值 |
|---|---|
| 地址 | **http://<NAS_LAN_IP>:923** |
| 密码 | **<WEB_PASSWORD>** |
| 容器名 | `TickFlow_Stock_Panel` |
| 镜像 | `tsp-nas:0.3.45` |
| 部署目录 | `/volume1/docker/TSP` |

同一局域网的手机、平板、电脑都能直接打开。首次进入会看到「使用前须知」引导页（1/5），点完即可进主界面。

> **升级后页面还显示旧版本号？** 这是浏览器标签页的缓存问题（前端版本查询
> `staleTime: Infinity`，一次加载后不会自动重取）。**按 `Ctrl+Shift+R` 强制刷新**即可。
> 服务端版本一直是新的：`curl http://<NAS_LAN_IP>:923/api/data/version` → `{"version":"v0.3.45"}`。

---

## 二、目录结构（NAS 上）

```
/volume1/docker/TSP/
├── docker-compose.yml      # 编排文件（端口 923、数据卷、legacy-cpu）
├── .env                    # AUTH_PASSWORD='<WEB_PASSWORD>'（权限 600）
├── tiers.yaml              # 数据源档位配置（只读挂载）
├── data/                   # ★ 全部用户数据：行情K线、自选、策略、回测、auth.json
├── src/                    # 源码（构建上下文，35 MB）
├── build.log               # 本次构建完整日志
└── src.tar.gz              # （已删除，节省 22 MB）
```

**`data/` 就是你的全部数据**，备份它等于备份一切。重装/升级容器都不会动它。

---

## 三、为什么要自己构建镜像（不能直接拉官方镜像）

你的 NAS 是 **DS918+ / Intel Celeron J1900（Bay Trail，Silvermont 架构）**，CPU 指令集只有 **SSE4.2，连 AVX 都没有**。

polars 官方预编译轮子要求 **AVX2**，在 J1900 上会直接 `Illegal instruction`（进程 exit 132）。项目文档专门写了这个场景，解法是构建时传 `BACKEND_EXTRAS=legacy-cpu`，让依赖装 polars 的 **runtime-compat** 版本（运行时探测 CPU 能力）。

所以本机 `docker-compose.yml` 里写的是：

```yaml
build:
  args:
    BACKEND_EXTRAS: "legacy-cpu"
```

**实测结果**：镜像内 `polars 1.44.1` / `pyarrow 24.0.0` / `duckdb 1.5.3` / `pandas 3.0.3` 全部正常工作。

> 如果哪天你在别的 x86 机器（支持 AVX2）上部署，可以省掉自建，直接
> `docker run -d --name tsp -p 923:3018 -v /volume1/docker/TSP/data:/app/data ghcr.io/shy3130/tick-stock-panel:latest`。

---

## 四、常用运维命令

SSH 登录 NAS 后（用户名 `<SSH_USER>`），所有 docker 命令都要 `sudo`：

```bash
cd /volume1/docker/TSP

# 查看状态
sudo docker compose ps
sudo docker compose logs -f --tail 100      # 实时日志

# 启停 / 重启
sudo docker compose up -d
sudo docker compose restart
sudo docker compose down                     # 停止并删除容器（数据仍在 data/）

# 进容器排查
sudo docker exec -it TickFlow_Stock_Panel bash
```

也可以直接用群晖 **Container Manager** 图形界面：容器列表里找 `TickFlow_Stock_Panel`，能看到状态、日志、资源占用。

---

## 五、首次使用：先配数据源

和本地客户端一样，**面板自带空库**，界面能开但没有行情。顺序：

1. 左下角 **设置 → 数据源**，配置数据源
2. **数据** 里拉取日K
3. 之后自选 / 策略 / 因子 / 回测 才有数据

### 免费源已经帮你配好了

`v0.3.27` 起，下面三项已经在本机 `.env` 对应的 `data/user_data/preferences.json` 里设好，
**开箱即用，不用再点**：

| 能力 | 生效源 | 实测 |
|---|---|---|
| 分钟K | **新浪行情（免费）** | `/api/kline/minute` 返回 238 根（09:31–15:00），带成交额 |
| 五档盘口 | **腾讯行情（免费）** | 「连板梯队 → 立即修正」返回 `{"ok":true,"count":89}` |
| 全量分钟 | **新浪行情（免费）** | `/api/intraday/status` → `full_market`、5579 只、收盘 final_sync 完成 |

日K 仍走 TickFlow（你已配免费档），实时行情走 fuyao（你已配 Key）——
腾讯也能提供实时行情，想换就在 **设置 → 数据源 → 能力路由** 里点「实时行情」换源。

> 想在界面里自己改：**设置 → 数据源 → 能力路由**，点能力卡片上的源标签即刻生效；
> 或点数据源卡片上的「套用」，让它一次接管自己适配的全部能力。

启动日志会打印当前能力状态：

```
能力路由 | 日K       ← tickflow    可用
能力路由 | 除权因子   ← fuyao       可用
能力路由 | 实时行情   ← fuyao       可用
能力路由 | 分钟K     ← sina        可用
能力路由 | 五档盘口   ← tencent     可用
能力路由 | 财务数据   ← fuyao       可用
能力路由 | 全量分钟   ← sina        可用
```

日志里 `fuyao_api_key 未配置` 的告警是内置扩展表（概念板块/行业板块）要的 key，不影响主功能。

### 两个免费源的口径说明（重要）

- **单位**：`volume` 一律是「手」，`amount` 是「元」，`change_pct`/`turnover_rate` 是小数制
  （0.004 = 0.4%），与 TickFlow 口径一致，界面换算不用管。
- **分钟K 深度**：新浪上游最多给约 4 个交易日（`datalen` 上限 1023）。更早的拉不到。
- **每日根数**：个股/ETF 是 238 根（09:31–15:00，收盘集合竞价没有独立分钟），
  指数是 239 根（尾部多一根 14:58）。
- **停牌**：停牌股的开/高/低/量/额返回空值，不会伪造成 0。
- **指数无盘口**：指数以及部分标的没有五档，此时盘口返回空，不是 bug。
- 这两个源是**只读行情接口**，不涉及账号、不需要 Key、不消耗积分。

### 模拟盘、策略、风险雷达在 NAS 上都是新的（v0.3.28 / v0.3.29）

| 功能 | 位置 | 状态 |
|---|---|---|
| 24 个 zdk 选股策略 | 策略 → 策略池 | 已部署，`/api/strategies` 返回 24 个、load_errors 为空 |
| 策略池删除 | 策略 → 策略池 → 每行垃圾桶 | 可用 |
| 三档订单类型详解 | 模拟盘 → 展开账户 → 下单区「这三档是什么意思?」 | 可用 |
| 撮合口径默认开启 | 同上，详解面板内可切换 | 新建账户默认开；存量账户首次打开自动迁移 |
| 交易计划 | 模拟盘 → 展开账户 → 「交易计划」标签 | 可用 |
| 跟随大盘情绪缩放仓位 | 交易计划页顶部徽章 | 可用 |
| 风险雷达 + 盘前简报 | 情绪周期 → 情绪周期页签顶部两张卡 | 可用（台湾加权在 NAS 上缺，界面标注「缺失」） |
| 风险变化主动提示 | 右上角提示条 + 监控中心 + 系统通知 | 可用（来源标记为「风险提示」） |
| 仓位自动跟随大盘/外围 | 交易计划页顶部「自动跟随大盘」徽章 | 可用（策略仓位 × 大盘 × 外围 × 回撤） |
| 监控触发记录带名称与来源 | 监控中心 → 触发记录 | 可用（v0.3.30） |
| 账户多选批量启动/停止/删除 + 重置 | 模拟盘 → 对比视图首列复选框 | 可用（v0.3.30） |
| 大盘择时 + 综合建议仓位 | 情绪周期 → 页签顶部 | 可用（v0.3.30） |
| 选股结果 → 交易计划 | 策略 → 结果表勾选 → 「生成交易计划」 | 可用（v0.3.30） |
| 计划执行方式三选一（区间/次日开盘/即时） | 模拟盘 → 展开账户 → 交易计划 → 新建计划 | 可用（v0.3.31） |
| 看板四卡（盘前简报/大盘择时/情绪周期/风险雷达） | 看板首页 → 情绪雷达上方 | 可用（v0.3.31） |
| 监控中心降噪（隐藏自动跟单） | 监控中心 → 页头开关 | 可用（v0.3.31，默认隐藏自动跟单） |
| 优中选优（持仓数上限 + 按评分取前 N） | 策略 → 结果表勾选 → 「生成交易计划」 | 可用（v0.3.31） |
| 账户详情「今日决策」卡 + 详细计划表 | 模拟盘 → 点行展开 → 交易计划页签 | 可用（v0.3.31） |
| 手机端二级界面响应式 | 策略 → 卡片「策略设置」 | 可用（v0.3.32，390×844 无横向溢出） |
| 交易计划弹窗写清「为什么买不了」 | 策略 → 结果表勾选 → 「生成交易计划」 | 可用（v0.3.32，逐行中文原因 + 汇总块） |
| 看板四卡一卡一色 | 看板首页 → 情绪雷达上方 | 可用（v0.3.32，蓝/琥珀/红/绿） |

**NAS 实测数字**（v0.3.32，容器重建后）：

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.32","mode":"free"}` |
| `/api/strategies` | `strategies=24`、`load_errors=0` |
| `/api/paper/plan_config` | `entry_modes=["zone","next_open","market"]`、`default_entry_mode="zone"`、`max_symbols=0`、`min_holdings=1`、`max_total_exposure_pct=100.0`、`top_n=10`；`holdings={limit:null, unlimited:true, timing_exposure:0.35, phase_exposure:1.0, reason:"未设置只数上限 (max_symbols=0, 不限制)"}`；`timing={state:"risk_off", label:"空头趋势", exposure:0.35, ok:true}` |
| `/api/risk/timing` | `state=risk_off`、`exposure=0.35`、`ma20=4181.679`、`ma60=4450.626`、`stale_days=4` |
| `POST /api/paper/plans/preview` | 3 行 → `summary={total:3, ok:2, blocked:1, total_pos_pct:12.0, need_cash:23250.0}`、`equity=200000.0`；600000.SH `ok=true` 区间 9.8~10.2 止损 9.76 仓位 6.0% 1200 股；600519.SH `ok=false` **仍给区间 1470~1530 / 止损 1464**，reason「计划股数不足一手 (按仓位 6.0% 只能买 0 股), 一手 150000.0 元; 提高仓位上限、换成低价标的, 或给账户加资金」；预览**不落盘**；101 行 → 400；`entry_mode="xxx"` → 400 |
| `POST /api/paper/plans/from_rows` | 4 行 + `top_n=2` → `created=2`（600519.SH / 000001.SZ），另 2 条 skipped「未进入优中选优前 2 名, 未生成计划」；重复提交 → `created=0` 且 2 条「已有活跃计划, 未重复建单」；`entry_mode="xxx"` → **400「entry_mode 只能是 zone/next_open/market, 收到 'xxx'」** |
| `/openapi.json` | 含 `plans/from_rows`、`plans/preview`、`accounts/batch_delete`、`accounts/batch_status`、`accounts/{account_id}/start|stop`、`/api/paper/reset`、`/api/risk/timing`、`/api/risk/advice`、`/api/paper/plan_config` |

> **v0.3.31 基线**：`/api/risk/radar` → `score=66.1 level=elevated exposure=0.79`；
> `/api/risk/advice` → `suggested_exposure_pct=27.7 label=轻仓试单`。

**NAS 接口实测（v0.3.39，rebuild 后 23:44–23:53）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.39","mode":"free"}`（build 76.3s，冷启动约 40s 就绪，轮询 attempt 12） |
| `/api/data/version` | `{"version":"v0.3.39"}` |
| `/api/regime/latest` | **`row.date=2026-11-04`、`state=strong`、`score=83`、`phase=repair`**（情绪周期当天） |
| `/api/regime/coverage` | `rows=244`、`latest_date=2026-11-04` |
| `/api/risk/timing` | `ok=true`、`symbol=510300`、**`as_of=2026-11-04`**、`stale_days=0`、`state=risk_off`、`bars=120` |
| `/api/risk/radar` | `as_of=2026-11-04`、`stale_days=0`、`score=32.7`、`exposure=1.1` |
| `/api/risk/premarket` | `as_of=2026-11-04`、`bias=偏多` |

> 注意 `/api/regime/latest` 的返回**不是扁平的**，数据在 `row` 层：
> `{"row": {"date": "...", "state": "...", ...}}`，直接读顶层会全是 None。

> **0.3.39 部署记录**：
> 1. 上传 **7 个文件**全部 md5 OK：`backend/app/__init__.py`、`backend/app/main.py`（36328 B）、
>    `backend/app/services/market_timing.py`（26595 B）、`backend/app/services/index_daily_free.py`（7344 B）、
>    `backend/pyproject.toml`、`frontend/package.json`、`VERSION`。
> 2. 旧 `backend\dist\TSP` 本次**没有**被僵尸进程锁住（`Rename-Item` 试改成功），
>    仍按铁律先 `Get-Process TSP | Stop-Process -Force` 再删目录。
> 3. ISCC `/DMyAppVersion=0.3.39` → `TSP-Setup-0.3.39.exe` **135,734,396 B**（501s）。
> 4. 本地静默安装 INSTALL_EXIT=0 → 手动启动 → `/health` attempt 7 OK `0.3.39 mode=none`；
>    本地 `/api/risk/timing` → `as_of=2026-11-04 stale=0`、`/api/regime/latest` → `date=2026-11-04 lean_strong 66 ice`，
>    与 NAS 口径一致（本地 score 66 vs NAS 83 是各自 enriched 数据差异，非 bug）。

**NAS 接口实测（v0.3.38，rebuild 后 21:19–21:24）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.38","mode":"free"}`（build 71.1s，冷启动约 55s 就绪） |
| `/api/overview/market` | **`limit_up=121`、`limit_down=11`、`broken=36`、`seal_rate=77.07`、`max_boards=3`、`fake_up=1`、`sealed_ready=true`** —— 0.3.36 时为 34/2/45.33，0.3.37 只修第一层时为 105 |
| 本地安装包同一接口 | `limit_up=122`、`limit_down=12`、`seal_rate=77.22`、`fake_up=0`（与独立精确复算 122 完全一致；NAS 少 1 只是那只确实封单不足被 sealed 正确剔除） |

> **0.3.37/0.3.38 部署踩坑记录**：
> 1. **Windows 僵尸进程锁目录** —— 0.3.36 装好后启动的 `TSP.exe`（pid 18540）退出后变成僵尸
>    （`HasExited=True` 但内核仍持 130 个文件的句柄），`Stop-Process` / `taskkill /F` 都报
>    「没有正在运行的实例」，`backend\dist\TSP` 无法删除或改名。**绕开方案**：PyInstaller 产物
>    复制到 `backend\dist\TSP37` / `TSP38`，并复制一份 `packaging\tsp_033X.iss` 只改 `Source:`
>    指向新目录，用这份 iss 打 ISCC（**不改原 tsp.iss**）。装到 `D:\TSP` 的尝试也失败
>    （实际装到 `C:\Users\<user>\AppData\Local\Programs\TSP\`，`DefaultDirName` 被忽略）。
> 2. **上传清单**：0.3.37 传 5 个文件（含 `backend/app/indicators/pipeline.py` 119772 B），
>    0.3.38 传 6 个（含 `backend/app/services/market_overview_builder.py`、
>    `backend/app/api/screener.py`）；channel-cat + md5 全部 OK。
> 3. **验证脚本要读 enriched 不读 daily** —— `kline_daily` 没有 `raw_close` 列，必须读
>    `kline_daily_enriched`；逐分区 `pl.read_parquet` 直接 concat 会因 `quote_ts` 列漂移报
>    `ComputeError: schema lengths differ`，需逐分区 `select` 共同列后 `vertical_relaxed` 拼。

**NAS 接口实测（v0.3.36，rebuild 后 18:33–18:38）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.36","mode":"free"}`（build 71.7s，冷启动约 40s 就绪） |
| `/api/regime/latest` | **`date=2026-11-04 state=lean_strong score=67`**（需求1 核心：情绪周期与当天日期一致；0.3.35 时停在 11-03 因 watchdog 误判自杀重启） |
| `/api/risk/timing` | `state=risk_off as_of=2026-11-04 stale_days=0 bars=120`（自愈未回归） |
| `/api/risk/radar` | `as_of=2026-11-04 stale_days=0 score=43.9` |
| `/api/risk/premarket` | `as_of=2026-11-04 bias=震荡` |
| `/api/paper/accounts` | 20 个账户；全部 holdings=0、active plans=0（当日无成交 → 买入区间列显示 `--` 软降级，属预期） |
| `/api/strategies` | `strategies=24`、`load_errors=[]` |
| `/api/paper/plan_config` | `entry_modes=[zone,next_open,market]`、`default=zone`（未回归） |

> **0.3.36 上传方式**：沿用 channel `exec_command("cat > …") + makefile_stdin` 字节流写法
> （`upload_0336.py`，10 文件 md5 全 OK）。**部署脚本/tests 不进 NAS**（镜像 pnpm build 不跑 vitest）。

**NAS 接口实测（v0.3.35，rebuild 后 15:43–15:47）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.35","mode":"free"}`（rebuild 95.6s，起容器后 /health 首次轮询即就绪） |
| `/api/risk/timing` | `state=risk_off`、**`as_of=2026-11-04`、`stale_days=0`、`bars=120`**（免费源回补生效，与当天日期一致；0.3.34 时停 2026-10-30 stale=5） |
| `/api/paper/overview?account=…` | holdings 5 行、`position_pct` 7.54/9.35/9.85…；`plan_*` 暂空 —— 历史存量持仓无计划属预期，`ensure_plan_for_fill` 只对新成交自动建计划 |
| `/api/strategies` | `strategies=24`、`load_errors=[]` |
| `/api/paper/plan_config` | `entry_modes=[zone,next_open,market]`、`default=zone`（未回归） |

> **0.3.35 部署踩坑记录**：`rebuild_0335.py` 第一版把 `docker compose build` 的工作目录写成了
> `src/tick-stock-panel-main`（那里没有 docker-compose.yml），构建空跑、容器仍是 0.3.34；
> 正确 compose 目录是 **`/volume1/docker/TSP`**。改回后 build 95.6s → up -d → 0.3.35 Up。

**NAS 接口实测（v0.3.34，rebuild 后 12:16–12:27）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.34","mode":"free"}`（冷启动预热约 2 分钟，轮询 40 次×3s 后就绪） |
| `/api/paper/overview?account=…` | holdings 行新增 **`position_pct`**（7.61 / 9.18 / …，= 市值÷总资产×100）；有活跃计划时并返回 `plan_stop_loss/plan_target1/plan_target2/plan_entry_mode/plan_source`（无计划时不带这些键，软降级） |
| `/api/alerts` | 新事件 message 前缀生效：`模拟盘[游资·龙回头回踩]买入 000700.SZ …`；`auto_order` 事件带 `account_name`（历史旧事件不含该字段，属预期） |
| `/api/strategies` | `strategies=24`、`load_errors=[]` |
| `/api/paper/plan_config` | `entry_modes=[zone,next_open,market]`、`default=zone`（0.3.31 功能未回归） |
| `/api/risk/radar` | `as_of=2026-11-04`、`stale_days=0`、`score=65.4`、`exposure=0.80`（0.3.33 日期功能未回归） |
| `/api/risk/premarket` | 顶层 `as_of=2026-11-04`、`us=2026-11-03`、`a50=2026-11-04`、bias=偏空 |
| `/api/risk/timing` | `state=risk_off`、`as_of=2026-10-30`、`stale_days=5`（同 0.3.33，真实数据滞后） |

> **0.3.34 部署踩坑记录**：首传清单漏了 `frontend/src/lib/api.ts`（`PaperHolding` 新增 6 字段的类型定义在那里），
> NAS 端 `pnpm build` 报 TS2339（`Property 'position_pct' does not exist on type 'PaperHolding'`）构建失败。
> 补传后 md5 MATCH、重建成功。教训：**前端类型改动必须连带 api.ts 进上传清单**。

**NAS 界面实测（v0.3.34，`_verify\shot_nas_0334.py` CDP 截图）**：
- 截图 `_verify\nas_holdings_0334.png`（390KB）：模拟盘展开「突破新高」账户，持仓表 12 列齐全 ——
  代码/数量/可卖(T+1)/成本/现价/**仓位%**/市值/盈亏/**止损/目标1/目标2/来源**，5 只股票行渲染正常；
  止损/目标列因该账户无活跃计划显示 `--`（软降级正确），仓位% 列有值（7.61/9.18/9.91/9.92/8.04）。
- 监控中心历史事件无 account_name 徽章（旧数据，新成交事件才带），文案前缀已由 API 层 + vitest 用例覆盖。

**NAS 界面实测（v0.3.33，headless Edge 走 CDP 截图）**：
- `_verify\nas_cards_0333.png`（1600×2400 / 423 KB）—— 侧栏 `设置 v0.3.33`；
  看板顶部四卡同排（top=212，各宽 331），**每张卡都标了数据日期**：

  | 卡片 | 日期行 | 滞后徽标 | 实测内容 |
  |---|---|---|---|
  | 盘前简报 | `预判日 2026-11-04` | — | `S=-0.38` 偏空 弱、缺口 -0.7%~+0.4%、高开 23.9% / 平开 26.8% / 低开 49.3%；三个驱动源各带自己的日期：A50 期货 -0.08% **2026-11-04**、美股隔夜 +0.45% **2026-11-03**、亚太同期 -2.79% **2026-11-04** |
  | 大盘择时 | `数据 2026-10-30` | **滞后 5 天** | `000300.SH` 空头趋势、择时系数 ×0.35、收盘 3985.50、MA20 4181.68 (-4.69%)、MA60 4450.63 (-10.45%)、建议仓位 15.0% |
  | 情绪周期 | `数据 2026-11-03` | — | 修复 弱势、情绪系数 ×0.30、综合分 22、涨停/跌停 109/39、最高板 7 板 |
  | 风险雷达 | `数据 2026-11-04` | — | 偏高、仓位系数 ×0.78、风险分 67.1；美股 45.1 ×1.05 / 亚太 100.0 ×0.35 / 恒生 59.2 ×0.88、下跌占比 54.0% |

  **「滞后 5 天」是真实情况**（本地 `000300.SH` 日线还没回补到最新交易日），
  以前这一条只能靠 `T-5` 这种暗语猜，现在直接写中文。
  四张卡都不含 `日期未知` —— 说明后端把 `as_of` 全给齐了。

**NAS 接口实测（v0.3.33，`_verify\nas_0333_check.py`，27 项断言全过）**：

| 接口 | 实测值 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.33","mode":"free"}` |
| `/api/risk/radar` | `as_of="2026-11-04"`、`stale_days=0`、`score=65.6`、`level="elevated"`、`exposure=0.797`；7 个指数**每行都有 as_of**：`DJIA/IXIC/SPX=2026-11-03`、`N225/KS11/HSI=2026-11-04`、`TWII=2025-09-26`（停更，仍被标出真实日期） |
| `/api/risk/premarket` | 顶层 `as_of="2026-11-04"`、`bias="偏空"`；`a50=2026-11-04`、`us=2026-11-03`、`asia=2026-11-04`、`cnh=2026-11-04` |
| `/api/risk/timing` | `as_of="2026-10-30"`、`stale_days=5`（既有字段未受影响） |
| `/api/paper/plan_config` | `labels.entry_modes=["zone","next_open","market"]`、`labels.default_entry_mode="zone"`、`holdings={limit:null, unlimited:true, max_symbols:0, min_holdings:1, timing_exposure:0.35, phase_exposure:0.3}`、`timing={state:"risk_off", label:"空头趋势", exposure:0.35, ok:true}` |
| `/api/strategies` | `strategies=24`、`load_errors=[]` |

> **`radar.fetched_at` 仍是 `"2026-11-04 10:29:32"` 这种时间戳**（没被 `as_of` 顶替）——
> 这是特意用测试锁住的：`fetched_at` 回答「什么时候抓的」，`as_of` 回答「数据是哪天的」，两回事。

**NAS 界面实测（v0.3.32，headless Edge 走 CDP 截图）**：
- `_verify\nas_cards_0332.png`（1600×2400 / 421 KB）—— 侧栏 `设置 v0.3.32`；
  看板顶部四卡同排（top=212，各宽 331），**一卡一色**：
  盘前简报（蓝 `accent`）、大盘择时（琥珀 `warning`）、情绪周期（红 `bull`）、风险雷达（绿 `bear`）。
  实测文本：盘前简报 `S=+0.03 震荡`、大盘择时 `空头趋势 择时系数 ×0.35 T-5 建议仓位 15.0%`、
  情绪周期 `修复 弱势 情绪系数 ×0.30 综合分 22`、风险雷达 `偏高 仓位系数 ×0.92 风险分 55.9`。
- `_verify\nas_plan_0332.png`（1600×2400 / 540 KB）—— 「生成交易计划」弹窗：
  权益 `200,000.00`、**「大盘择时 空头趋势 · 仓位 ×0.35」**（不再是情绪周期口径 ×1）、
  「情绪周期 弱势 · 仓位 ×0.3」两个独立徽标，「生成交易计划 (2)」。
- `_verify\nas_mobile_0332.png`（390×844 / 171 KB）—— 手机视口下策略二级界面：
  `scrollWidth == clientWidth == 390`（**无横向溢出**）、竖排元素 0 个、左侧导航已变顶部横向标签（`flex-direction: row`）。
- `_verify\local_low_equity.png`（1600×1500 / 507 KB，本地客户端）—— **低资金场景**：
  20 元账户下两行都是「买不了」，但**不再是一片灰**：买入区间 21.52~22.40、止损 20.13 (-8.33%)、
  目标1 24.71 / 目标2 27.45、仓位 12.00%、盈亏比 1.5R 全部照常显示，
  备注列写「计划股数不足一手 (按仓位 12.0% 只能买 0 股), 一手 2196.0 元; 提高仓位上限、换成低价标的, 或给账户加资金」，
  弹窗底部还有「⚠ 2 只无法生成计划（共 2 只）。常见原因: 账户资金不足以买一手、缺价格/ATR 数据、止损距离无效…」汇总块。

**NAS 界面实测（v0.3.31，headless Edge 走 CDP 截图）**：
- `_verify\nas_dashboard_0331.png`（1600×2400 / 403 KB）—— 侧栏 `设置 v0.3.31`；
  看板顶部四卡同排（top=212），在情绪雷达（top=554）上方：
  **盘前简报**（S=+0.04 震荡 弱）、**大盘择时**（000300.SH 空头趋势 择时系数 ×0.35 T-4
  建议仓位 27.7% 轻仓试单）、**情绪周期**（2026-11-02 **偏强** 情绪系数 ×1.00 综合分 67）、
  **风险雷达**（偏高 仓位系数 ×0.79 风险分 66.1）；三卡都写明
  「在建仓时乘到买入股数上…**卖出(止损/止盈/超时)永不受影响**」。
  （情绪周期卡显示中文「偏强」——0.3.31 打包时修掉了它原来直接渲染 `lean_strong` 枚举的 bug。）
- `_verify\nas_paper_0331.png`（1600×2400 / 319 KB）—— 模拟盘 21 个账户；点行展开 →
  「交易计划」页签 → **今日决策卡**：「更新于 21:48:29 大盘 空头趋势 000300.SH 3985.50 /
  MA20 4181.68 -4.69%」「当前仓位 0.0% / **今日上限 27.7%（满仓=100%）· 轻仓试单**」
  「计划 0 只 · 计划仓位 0.0%」「风险雷达 偏高(66.1分) → 系数 0.79」，
  口径行注明「今日上限取自 /api/risk/advice 的综合建议仓位(择时 × 情绪周期 × 外围风险,
  已夹 15%~120%)」以及「**卖出不受大盘择时影响**」。
- `_verify\nas_monitor_0331.png`（1600×2400 / 438 KB）—— 监控中心页头有
  「已隐藏 自动跟单 · 点开关可改」+「成交」两个开关，默认隐藏自动跟单、保留成交。

> **大盘情绪（市场环境）在 NAS 上要先有数据**。「交易计划」的仓位倍率与止损收紧读的是
> `data/regime_history/part.parquet`。如果这个文件不存在，计划照常工作，只是倍率恒为 1.0（不缩放）。
> 补算方法见下面「补算市场环境」。

#### 补算市场环境（一次性，约 1 分钟）

```bash
sudo docker exec TickFlow_Stock_Panel curl -s -X POST http://127.0.0.1:3018/api/regime/recompute
```

返回 `{"ok":true,"computed":242,...}` 即写入 242 个交易日的历史。之后
`curl -s http://<NAS_LAN_IP>:923/api/regime/latest` 能看到最新状态（例如
`{"date":"2026-11-02","state":"lean_strong","score":67}`）就说明好了。

#### 交易计划怎么用

1. 模拟盘 → 展开一个账户 → 顶部切到 **「交易计划」**；
2. 点 **「从策略生成计划」** 选一个策略（如 `zdk_breakout`），填生成条数，确认；
3. 表格里每条计划有：买入区间、止损、目标1、目标2、仓位%、计划数量、成本、盈亏、盈亏比；
4. 价格进区间会自动买入、到点位自动卖出（由行情轮询钩子驱动，无需你盯盘）；
5. 参数可在 **「计划参数」** 里调（单笔风险%、仓位上下限、ATR 倍数、最大止损幅度、
   买入区间宽度、目标 R 倍数、最长持有天数、两个跟随大盘开关）。

> **成交前提**：交易计划依赖「即时」订单撮合，所以在**交易时段内**才会触发买入/卖出；
> 收盘后触发的动作会在下一个交易日继续判定。

### 风险雷达与盘前简报（v0.3.29）

位置：**情绪周期 → 情绪周期页签**最上面两张卡（先点「情绪周期」标签，默认在「市场基础」）。

| 卡片 | 内容 | 数据来源 |
|---|---|---|
| **盘前简报** | 偏多/震荡/偏空 + 强/中/弱，高开/平开/低开三档概率，预计缺口区间，预计振幅，A50、美股、亚太涨跌，离岸人民币（仅提示） | 新浪 `hq.sinajs.cn`（A50 期货 / 美股三大 / 日经·KOSPI / USDCNH） |
| **风险雷达** | 仓位系数 ×0.35~×1.10 + 低/正常/偏高/高，美股 / 亚太 / 恒生三个区域分，7 个指数明细（含 MA20 偏离），A 股涨跌家数，账户回撤 | 新浪全球指数 + 东财日线（算 MA20）+ 本地 enriched 分区（宽度）+ 账户净值（回撤） |

**怎么读**：分数 50 是中性，越高越危险；仓位系数是**要乘到你计划仓位上的倍率**，
高风险时最低 ×0.35（**不会算成 0**，永远不会空仓）。外围只是风险温度旁证，
不是「美股跌了就空仓」—— 美股与 A 股日收益相关性只有 0.2~0.3，所以权重温和缩放。

**主动提示**：风险等级跨档、仓位系数跳变 ≥0.10、分数变化 ≥8、盘前方向翻转、大盘状态换档，
都会产出一条提示（来源标记 **风险提示**）：右上角提示条 + 监控中心 + 系统通知 + 飞书/企业微信。
同一档 30 分钟内不重复，全局最快 120 秒一次。首次运行会给一条基线播报。

> **NAS 上的已知差异**：台湾加权（TWII）在 NAS 容器内取不到 —— 东财 `push2` / Yahoo 在 NAS
> 网络环境被阻断，新浪 `znb_TWSE` 停在 2025-09-26。界面上该行显示「缺失」，风险分会按其余
> 6 个指数重新归一，**不影响使用**。
>
> 日经与恒生的 **MA20** 在 NAS 上用「新浪全球期货日线」（NK / HSI）兜底，与现货偏离同方向
> 同量级，可用；韩国 KOSPI 没有对应期货，那一项的 MA20 会留空（当日涨跌幅照常）。
> 本地 Windows 客户端上东财通常可用，7 个指数都能取到。

---

## 六、改密码

两种方式：

1. **页面里改**（推荐）：登录后 **设置 → 修改密码**
2. **重置**（忘了密码时）：
   ```bash
   cd /volume1/docker/TSP
   sudo docker compose down
   sudo rm data/user_data/auth.json
   sudo docker compose up -d
   ```
   删除后重启会回到"未设密码"状态，此时 `.env` 里的 `AUTH_PASSWORD` 会**重新生效**一次。

> `.env` 里的 `AUTH_PASSWORD` 是**一次性引导**：启动时读一次、哈希后写进 `auth.json`，之后就不再看它了。所以你在页面改的密码不会被重启覆盖。

---

## 七、升级到新版本

源码在 `/volume1/docker/TSP/src/tick-stock-panel-main`。上游更新后：

```bash
cd /volume1/docker/TSP/src/tick-stock-panel-main
sudo git pull            # 若当初是 tarball 解压的（无 .git），则重新下载解压覆盖
cd /volume1/docker/TSP
sudo docker compose build      # 约 20-25 分钟（J1900 较慢）
sudo docker compose up -d
```

`data/` 不受影响，`git pull` / 重建镜像都不会碰它。

---

## 八、注意事项

- **数据安全**：`data/` 是唯一数据源。想稳妥的话在群晖 **Hyper Backup** 或 **Cloud Sync** 里把这个目录加进去。
- **公网访问**：现在只映射到局域网。如果要暴露到公网，**务必**先改掉默认密码 <WEB_PASSWORD>，并且建议走群晖反向代理 + HTTPS，不要直接把 923 端口映射出去。
- **内存占用**：面板常驻约 400 MB RSS（`/health` 里有实时数据），NAS 7.8 GB 内存无压力。
- **端口冲突**：923 已被本容器占用；NAS 上其它服务用了 9778 / 18760 / 3100 / 3000-3001 / 9097，无冲突。
- **开机自启**：`restart: unless-stopped` 已配好，NAS 重启后容器自动拉起（依赖 Container Manager 服务开机启动，群晖默认开启）。

---

## 九、已应用的本地修改

本目录的 `src/` 不是纯上游代码，有多处本地改动。**重新下载上游 tarball 覆盖 `src/` 会全部丢失**，
需要按下面的说明重新应用。

### 9.0 选股→交易计划 + 账户批量操作 + 大盘择时（v0.3.30）

| 文件 | 改动 |
|---|---|
| `backend/app/services/market_timing.py` | **新增** —— 沪深300 vs MA20/MA60 四态择时 + 三路合成建议仓位（只读本地，绝不联网） |
| `backend/app/api/risk.py` | 加 `GET /api/risk/timing`、`GET /api/risk/advice` |
| `backend/app/api/alerts.py` | 触发记录查询时按 symbol 回填缺失的 `name`（历史记录也能显示股票名称） |
| `backend/app/api/paper.py` | 加 `plans/from_rows`、`accounts/batch_delete`、`accounts/batch_status`、`accounts/{id}/start|stop`、`reset`（需 `confirm=true`） |
| `backend/app/strategy/paper.py` | 新增 `reset_account()`；`evaluate_intraday`/`settle_day` 补「冻结账户直接跳过」 |
| `backend/app/strategy/paper_plans.py` | `create_plans_from_rows` 返回 `skipped` 明细；`evaluate_plans` 冻结时短路 |
| `backend/app/strategy/paper_auto.py` | 自动跟单事件补 `name/rule_name/strategy_id/account_name/reason/order_type`，新增 `format_auto_order_message()` |
| `backend/app/services/quote_service.py` | 新增 `_enrich_paper_alert_names()`，推送前统一回填名称并重建文案 |
| `frontend/src/components/screener/StrategyPlanDialog.tsx` | **新增** —— 选股结果 → 交易计划预览/生成弹窗 |
| `frontend/src/components/screener/ScreenerTable.tsx` | 结果表首列复选框 + 表头全选 |
| `frontend/src/pages/Screener.tsx` | 行选择 state + 工具条「生成交易计划」按钮 |
| `frontend/src/components/regime/MarketTimingCard.tsx` | **新增** —— 大盘择时 + 综合建议仓位卡 |
| `frontend/src/pages/Regime.tsx` | 挂载上面这张卡（只加 import + 一行） |
| `frontend/src/custom/paper/Page.tsx` | 多选批量启动/停止/删除、`重置账户`、冻结按钮语义化为「启动运行/停止运行」 |
| `frontend/src/custom/monitor/Page.tsx` | 触发记录补来源标签与账户名副行 |
| `frontend/src/components/AlertToast.tsx` | paper 文案改两行显示，避免长文案被截断 |
| `frontend/src/lib/voiceBroadcast.ts` | 新增 `sanitizeForSpeech()`，剔除代码/英文 id 再念 |
| `frontend/src/lib/api.ts`、`frontend/src/lib/queryKeys.ts` | 补 6 个 paper 方法、2 个风险方法、2 个查询键 |

### 9.1 风险雷达 + 盘前简报 + 模拟盘自动仓位（v0.3.29）

| 文件 | 改动 |
|---|---|
| `backend/app/services/global_risk.py` | **新增** —— 外围 7 指数取数（新浪 + 东财兜底）、打分、仓位映射、A 股宽度、账户回撤 |
| `backend/app/services/premarket.py` | **新增** —— 盘前简报（A50/美股/亚太 → 开盘缺口三档概率） |
| `backend/app/services/risk_alert.py` | **新增** —— 风险提示中心（变化检测 / 频控 / 四路分发 / 状态落盘） |
| `backend/app/api/risk.py` | **新增** —— 4 个路由 `/api/risk/{radar,premarket,refresh,health}` |
| `backend/app/main.py` | 注册 risk 路由；关停时释放 `risk_alert` 线程池 |
| `backend/app/services/quote_service.py` | `SOURCE_LABELS` 加 `"risk": "风险提示"`；轮询线程每轮调 `risk_alert.schedule(...)`（独立线程池，不阻塞撮合） |
| `backend/app/jobs/daily_pipeline.py` | 新增 Step 2.9：盘后补推一次风险提示（收盘后轮询已停，这是唯一补推点） |
| `backend/app/strategy/trade_plan.py` | 新增 `position_adjust(...)`（大盘×外围×回撤三系数）；cfg 加 `max_symbols` / `max_total_exposure_pct`（默认 0 / 100 即不限） |
| `backend/app/strategy/paper_plans.py` | 买入数量改为 `计划仓位 × 三系数`；新增 `_radar_coeffs`（60 秒缓存，无 pending 计划时零联网） |
| `backend/app/strategy/paper.py` | `create_order` 新增 keyword-only `max_position_symbols`（默认 None 即不限只数） |
| `frontend/src/components/regime/GlobalRiskCard.tsx` | **新增** —— 风险雷达卡 |
| `frontend/src/components/regime/PremarketCard.tsx` | **新增** —— 盘前简报卡 |
| `frontend/src/pages/Regime.tsx` | 挂载两张卡（2 行 import + 2 行组件） |
| `frontend/src/components/AlertToast.tsx` | `SOURCE_BADGE` 加 `risk: 风险提示` |
| `frontend/src/lib/voiceBroadcast.ts` | 风险事件语音播报直接念 message |
| `frontend/src/lib/api.ts` / `queryKeys.ts` | 风险相关类型与 3 个 API、2 个 query key；交易计划新增字段类型 |
| `frontend/src/custom/paper/PlanPanel.tsx` | 计划参数面板加两项「人为闸门」（默认不限）+ 兜底默认值 |

改法：用 `_deploy\upload_0329.py`（28 个文件，逐文件 md5 校验）推到
`/volume1/docker/TSP/src/tick-stock-panel-main/` 对应位置，再 `docker compose build && up -d`。

> **注意**：`upload_0329.py` 复用了 `_deploy\nas.py` 里的 `connect()` / `run()`，
> SSH 账号密码在 `nas.py` 里（`USER="<SSH_USER>"`），**不是 Web 登录密码 <WEB_PASSWORD>**。

### 9.2 模拟盘交易计划 + 策略池删除（v0.3.28）

| 文件 | 改动 |
|---|---|
| `backend/app/strategy/trade_plan.py` | **新增** —— 纯函数的交易计划计算（ATR 止损、R 倍数目标、风险预算仓位、大盘缩放） |
| `backend/app/strategy/paper_plans.py` | **新增** —— 计划的存取与盘中对账（区间买入 / 止损 / 目标1减半 / 目标2清仓 / 超时） |
| `backend/app/strategy/paper.py` | `queue_limit_orders` 默认 False → True；新增一次性迁移 `migrate_queue_limit_defaults` |
| `backend/app/api/paper.py` | 新增 7 个 plan 路由（`/plans`、`/plans/from_strategy`、`/plan_config` 等）+ 账户接口懒触发迁移 |
| `backend/app/services/quote_service.py` | 行情钩子里插一行 `paper_plans.evaluate_plans(...)`（在 `evaluate_intraday` 之前） |
| `frontend/src/custom/paper/PlanPanel.tsx` | **新增** —— 交易计划面板 |
| `frontend/src/custom/paper/planClient.ts` | **新增** —— 计划 API 封装 |
| `frontend/src/custom/paper/Page.tsx` | 加「账户总览 / 交易计划」分段控件 + 订单类型详解弹层 |
| `frontend/src/components/screener/StrategyPoolDialog.tsx` | 策略库行加垃圾桶删除按钮 + 二次确认 |
| `frontend/src/lib/api.ts` | 追加 PaperPlan 等类型与 6 个计划 API |

改法：这台机器上 `G:\tick_stock_panel` 就是构建源，直接用
`_deploy\upload_0328.py`（逐文件上传 + md5 校验）把上述文件推到
`/volume1/docker/TSP/src/tick-stock-panel-main/` 对应位置，再 `docker compose build && up -d`。
从零重建 `src/` 的话，需要把 `G:\tick_stock_panel\{backend,frontend}` 整个覆盖过去。

### 9.3 新增两个免费数据源插件（v0.3.27）

| 插件 | 目录 | 提供能力 | 上游接口 |
|---|---|---|---|
| 腾讯行情（免费） | `backend/app/plugins/tencent/` | 实时行情、五档盘口 | `qt.gtimg.cn/q=` |
| 新浪行情（免费） | `backend/app/plugins/sina/` | 分钟K、全量分钟 | `quotes.sina.cn/.../CN_MarketDataService.getKLineData` |

两个都是 `runtime: none`（纯 HTTP，无新增依赖）。插件加载器自动扫描
`app/plugins/*/plugin.yaml` 注册，**不用改注册表、不用改前端**——
设置页的「能力路由」「数据源」卡片都是从插件清单自动派生的。

改法：把 `G:\tick_stock_panel\backend\app\plugins\{tencent,sina}\` 四个文件
（`plugin.yaml` / `provider.py` / `client.py` / `__init__.py`）整目录拷到
`/volume1/docker/TSP/src/tick-stock-panel-main/backend/app/plugins/` 下，
然后重新 `docker compose build`。

### 9.4 AI 助手手机窄屏修复

**问题**：手机上呼出 AI 助手后，抽屉左侧内容被裁掉（「所有策略」显示成「有策略」）。

**根因**：`frontend/src/custom/assistant/ui/AssistantDrawer.tsx` 里抽屉宽度下限写死 480px：

```ts
const MIN_WIDTH = 480
const maxWidth = Math.max(MIN_WIDTH, window.innerWidth - 64)   // 390px 手机 → max(480, 326) = 480
return Math.min(Math.max(width, MIN_WIDTH), maxWidth)          // 结果仍是 480
```

抽屉是 `right: 0` 贴右的，390px 视口塞一个 480px 面板 → 左侧溢出 90px 被裁。

**修复**（同一文件，`clampWidth`）：

```ts
const viewport = window.innerWidth
const minWidth = Math.min(MIN_WIDTH, viewport)      // 窄屏时放开下限
const maxWidth = Math.max(minWidth, viewport - 64)
return Math.min(Math.max(width, minWidth), maxWidth)
```

窄屏下抽屉贴合视口全宽，桌面端行为完全不变（1440px 视口仍是 720px 宽）。

**验证**（headless Edge + CDP 移动视口实测，不是估算）：

| 视口 | 抽屉宽度 | 左边界 | 右侧越界 | 结论 |
|---|---|---|---|---|
| 390×844（iPhone 14） | 390 | 0 | 否 | 通过 |
| 430×932（iPhone 14 Pro Max） | 430 | 0 | 否 | 通过 |
| 1440×900（桌面） | 720 | 720 | 否 | 通过（无回归） |

截图：`_verify\drawer_iphone14.png`、`_verify\nas_iphone14.png`（NAS 实测）。

---

## 十、构建过程记录（排障用）

| 阶段 | 耗时 | 备注 |
|---|---|---|
| 拉基础镜像 | ~1 min | python:3.11-slim / node:20-alpine，走群晖内置镜像加速 |
| apt 装 tesseract-ocr | 578 s | 走阿里云 debian 源（Dockerfile 的 `USE_CN_MIRROR=1`） |
| pnpm install（242 包） | ~240 s | npmmirror |
| pnpm build（vite） | ~220 s | |
| uv sync --extra legacy-cpu | 51 s | tuna 源 |
| 导出镜像层 | 93 s | btrfs |
| **首次全量合计** | **约 22 分钟** | J1900 4 核无 AVX |
| **v0.3.27 增量重建** | **8.5 分钟** | 只改了 `backend/app` 与 `frontend/package.json`，pnpm install 层命中缓存 |
| **v0.3.28 增量重建** | **11 分钟** | 改了 `backend/app` 6 个文件 + `frontend/src` 4 个文件（含新建 `PlanPanel.tsx`），pnpm install 层仍命中缓存 |
| **v0.3.29 增量重建** | **11 分钟** | 改了 `backend/app` 11 个文件 + `frontend/src` 9 个文件（含新建 `GlobalRiskCard.tsx`/`PremarketCard.tsx`），pnpm install 层仍命中缓存 |
| **v0.3.30 增量重建** | **约 11 分钟**（含 `vite build` 1m26s + 导出镜像 102s） | 改 `backend/app` 8 个文件 + `frontend/src` 15 个文件（含新建 `market_timing.py`/`StrategyPlanDialog.tsx`/`MarketTimingCard.tsx`），pnpm install 层命中缓存 |
| **v0.3.29 二次重建** | **约 3 分钟** | 只改了 `backend/app/services/global_risk.py`（加新浪期货日线的 MA20 兜底），其余层全部命中缓存 |

### v0.3.40 部署踩坑记录

1. **上传脚本必须用 dsh 的 python，不是 `backend\.venv`** —— `_deploy/nas.py` 依赖 `paramiko`，
   而 `backend\.venv` 里没装（`ModuleNotFoundError: No module named 'paramiko'`）。
   正确解释器：`C:\Users\jiangliuer\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
   （Python 3.12.14，已带 paramiko）。
2. **`frontend/src/lib/api.ts` 即使只改后端也要检查是否在清单里** —— 0.3.34 曾因漏传导致
   远程 `pnpm build` 报 TS2339/TS2353 整个构建失败（容器仍跑旧镜像）。
   0.3.40 只改后端 Python + 版本号，`api.ts` 无需上传（7 个文件：`main.py`、`market_timing.py`、
   `index_daily_free.py`、`__init__.py`、`pyproject.toml`、`package.json`、`VERSION`）。
3. **容器冷启动约 36s** —— `/health` 前 11 次失败、第 12 次成功，脚本要轮询不要固定 sleep。

### v0.3.41 部署踩坑记录

1. **别在请求路径里同步等网络** —— 0.3.41 第一版把「盘中刷新」也写成同步调用，
   页面上四张卡同时请求 → NAS 上 `/api/risk/timing` **180 秒超时**（30s 与 180s 两档都超）。
   排查方式：`nas.run(...)` 里对该接口单独发请求并显式给 timeout，看是卡住还是报错。
   修法见产品侧的「同步（缺数据）/ 异步（盘中未定型）」二分。
2. **并发重复抓取会放大成秒级事故** —— NAS 日志 `12:11:54`、`12:12:12`、`12:12:40`
   三次 `free backfill 510300 via tencent: +321 rows`，46 秒内打满 3 遍，
   每次 merge-upsert 321 个日分区、单次 20 秒起。加线程锁 + 锁内二次判断 + 盘中只写当天行。
3. **上传清单要含 `market_context_refresh.py`** —— 新增的 services 模块如果在清单外，
   远端 import 会 `ModuleNotFoundError`，容器起不来。本次 11 个文件。
4. **容器冷启动约 36s** —— `/health` 前 11 次失败、第 12 次成功，脚本要轮询不要固定 sleep。

### NAS 接口实测（v0.3.41，2026-11-05 盘中）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.41","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.41"}` |
| `/api/risk/timing` | `ok=True as_of=2026-11-05 intraday=True stale=0 state=risk_off bars=120` |
| `/api/regime/coverage` | `rows=245 latest=2026-11-05` |
| `/api/regime/latest` | `row.date=2026-11-05 state=strong score=79 phase=...` |

> 注：`/api/regime/latest` 的数据在 **`row`** 子对象里，不是顶层扁平字段（探测脚本容易踩）。
> 盘中 `intraday=True` 是 v0.3.41 新增字段，表示当天那根 K 还没收盘定型。

### NAS 接口实测（v0.3.42，2026-11-05 盘中）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.42","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.42"}` |
| `/api/risk/radar` | `indexes` 含 7 只单项（道琼斯/纳斯达克/标普500/日经225/首尔综合指数/TWII/香港恒生指数），每只含 `chg_pct`/`score`/`as_of`；看板风险雷达卡展开显示 |
| `/api/risk/premarket` | `asia.rows`/`us.rows` 含单指数涨跌幅；看板盘前简报卡展开显示 |
| 看板四卡 | 数据日期均为 `2026-11-05`，无滞后徽标；风险雷达卡显示「日经225 −1.79% / 首尔综合指数 −5.28% / 台湾台北指数 — / 香港恒生指数 +3.67%」，盘前简报卡显示「美股隔夜 道琼斯 +3.03% / 纳斯达克 +1.44% / 标普500指数 +1.58% 与 亚太同期 日经225 −1.79% / 首尔综合指数 −5.28% / TWII —」 |

> 注：v0.3.42 纯前端改造，后端接口契约（radar.indexes / premarket.asia.rows / premarket.us.rows）在更早版本已具备，本次只放开了看板卡片的展示层级。

### NAS 接口实测（v0.3.45，2026-11-06 20:29）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.45","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.45"}` |
| `/api/risk/timing` | `{"ok":true,"as_of":"2026-11-06","state":"risk_off","exposure":0.35,"bars":120,"stale_days":0}` —— **秒回**（修复前该接口超时 >120 秒） |
| `/api/regime/latest` | `{"date":"2026-11-06","state":"weak","score":5,"phase":"ignite"}` |
| `/api/risk/advice` | `{"ok":true,"suggested_exposure":0.15,"suggested_exposure_pct":15.0}` |
| `/api/paper/accounts` | 21 个账户（无回归） |
| 容器镜像 | `tsp-nas:0.3.45`，`TickFlow_Stock_Panel` Up |
| 镜像清理 | 已删除 `tsp-nas:0.3.26` ~ `tsp-nas:0.3.44` 共 19 个旧镜像（回收 158.8 MB），**只保留 `tsp-nas:0.3.45`** |

> **0.3.45 上传文件清单（12 个）**：`backend/app/services/index_daily_free.py`、
> `backend/app/services/market_timing.py`、`backend/app/market_time.py`、`backend/app/main.py`、
> `backend/tests/test_timing_freshness.py`、`backend/tests/test_index_daily_free.py`、
> `backend/tests/test_index_daily_free_backfill.py`、`frontend/src/components/dashboard/RiskWidgets.tsx`、
> `frontend/package.json`、`backend/app/__init__.py`、`backend/pyproject.toml`、`VERSION`。

> **0.3.45 部署踩坑（重要，已记录在案）**：
> 1. **`docker compose up -d` 的 Recreate 会卡死 J1900 的 dockerd** —— compose 的重建流程是把旧容器
>    改名成 `<hash>_TickFlow_Stock_Panel`，再「停旧容器 → 起新容器」。旧容器已经跑了 15+ 小时，
>    在 J1900 上停它超过 4 分钟，SSH 会话一断 compose 进程被杀，留下一个 `Created` 状态的残骸
>    和一个仍然在跑的旧容器，**daemon 也被这次卡住的 stop 拖住**（`docker ps` 时快时卡、
>    `docker kill` 直接悬住 20 分钟）。
>    对策（`_deploy/deploy_0345_final2.py` 与最后的 `/tmp/deploy0345.sh`）：**先把脚本写到 NAS 的
>    绝对路径**（`/tmp/xxx.sh` 或 `/volume1/docker/TSP/xxx.sh`），再
>    `sudo -S -p '' bash -c 'setsid nohup bash /tmp/xxx.sh >/dev/null 2>&1 </dev/null &'`
>    **脱离会话**执行，日志写文件后轮询读取。**不要**在 SSH 通道里直接跑耗时的 docker 命令。
> 2. **daemon 真的卡死时, 用 `synopkg restart ContainerManager`**（脚本同样要脱离会话执行）。本次它
>    悬了 6 分钟没返回，但实际已经成功重启（旧 dockerd PID 消失、新 dockerd 起来了）。
>    重启后**先 `docker rm -f` 掉所有 `TickFlow_Stock_Panel` 相关的旧容器与残骸**，再
>    `docker compose up -d` —— 旧容器不存在时 compose 走 create 而不是 recreate，秒级完成。
> 3. **不要在 NAS 上用 `docker stop`** 停长跑的 TSP 容器，用 `docker rm -f`（`stop` 要等优雅退出）。
> 4. **`~/xxx.sh` 在 `sudo bash -c` 里不可靠** —— sudo 会重置 `HOME`，`~` 指向 `/root`，而脚本
>    是普通用户写到自己的 home 下，结果 `bash: /root/xxx.sh: No such file or directory`。
>    **一律用绝对路径**。

### NAS 接口实测（v0.3.44，2026-11-06 00:45）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.44","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.44"}` |
| `/api/paper/accounts` | 21 个 arena 账户 |
| `/api/paper/auto_rules?account=<id>` | **21/21** 账户的跟单规则均含 `"position_mode":"equal_total"`（21/21 规则均带 `max_positions`，即只数缩放路径生效） |
| `/api/paper/plan_config` | 无回归（`max_symbols=0`、`max_pos_pct=20`、`max_total_exposure_pct=100`、`default_entry_mode="zone"`） |
| 容器镜像 | `tsp-nas:0.3.44`，`TickFlow_Stock_Panel` Up；`docker compose build` 耗时 267.5 s（前端 built in 1m 12s） |
| 容器内代码核验 | `docker exec TickFlow_Stock_Panel grep -n '_affordable' /app/app/strategy/paper_auto.py` → 命中 :233 / :236；`python -c "import app;print(app.__version__)"` → `0.3.44`（确认跑的是**修 bug 后**的代码，而非 0.3.43 初版） |

> **冷启动踩坑**：容器 `up -d` 之后立刻打 `/api/auth/login` 会被 **ConnectionResetError (WinError 10054)** 拒绝
> —— 这是容器还在起 Python 后端、端口已 listen 但 WSGI 尚未就绪。**必须轮询重试**（本项目用
> `_verify/nas_0344_check.py`：登录+`/health` 一起重试，最多等 300 s，间隔 6 s）。
> 之前 0.3.43 的验收脚本是固定 sleep 后再连，碰巧没撞上；这次撞上了。
>
> **0.3.44 上传文件清单（6 个）**：`backend/app/strategy/paper_auto.py`、
> `backend/tests/test_paper_auto_scaling.py`、`frontend/package.json`、`backend/app/__init__.py`、
> `backend/pyproject.toml`、`VERSION`。前端**无 src 改动**（0.3.44 是纯后端修复），但 Dockerfile 在镜像内
> 从 `frontend/` 源码 pnpm build，故仍需上传 `package.json` 让镜像内 `APP_VER` 取到 0.3.44。

### NAS 接口实测（v0.3.43，2026-11-05 22:10）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.43","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.43"}` |
| `/api/paper/accounts` | 21 个 arena 账户，全部 `status=active` |
| `/api/paper/auto_rules?account=<id>` | **21/21** 账户的跟单规则均含 `"position_mode":"equal_total"`（迁移前 21 个文件全部无该字段，默认 `fixed`） |
| `/api/paper/plan_config` | 无回归（`max_symbols=0`、`max_pos_pct=20`、`default_entry_mode="zone"` 等保持原值） |
| 容器内 `grep -rl equal_total /app/data/paper/accounts` | 21（磁盘文件确含新字段） |
| 容器镜像 | `tsp-nas:0.3.43`，`TickFlow_Stock_Panel` Up；`docker compose build` 耗时 278 s |

> **验证脚本坑（务必记住）**：自动跟单规则的真实路由是 **`GET /api/paper/auto_rules?account=<id>`**，
> **不存在** `/api/paper/account/<id>/auto_rules`（该路径返回非 JSON，用它校验会误判 equal_total=0）。
>
> **迁移脚本**：`_deploy/migrate_position_mode_0343.py` —— 遍历
> `/volume1/docker/TSP/data/paper/accounts/*/auto_rules/arule_*.json` 就地补写 `"position_mode":"equal_total"`，
> 保留其余字段。实测 `found 21 / migrated 21`。`load_auto_rules` 每次请求重读文件不缓存，迁移后即时生效（本次仍重建容器确认）。
>
> **视觉验收**（0.3.44 复核）：`_verify/probe_nas_0344_deep.py`（CDP）——
> 持仓表表头含 **「买入区间」** 列（`hasBuyZone=true`，28 行）；
> 「批量创建账户」弹窗内可见 **「仓位分配 / 固定每票 / 总仓均分」**，默认选中「总仓均分」。
> 截图：`_verify/nas_holdings_0344.png`、`_verify/nas_dlg_batch_0344.png`。
> 注意：该控件在**弹窗内**，主页面不出现「仓位分配」字样属正常。
>
> **「单账户」弹窗里的「仓位分配」要展开后才可见**（0.3.44 补充实测）：
> 该控件位于「事件跟单」参数区，而参数区只在**选中策略/监控规则（或输入搜索词）**后才展开。
> `_verify/probe_nas_0344_single.py` 实测：点「单账户」→ 在「搜索策略」框输入 `z` →
> `hasPositionModeLabel=true / hasFixed=true / hasEqualTotal=true`，
> 截图 `_verify/nas_dlg_single_expanded_0344.png`。
> 只点「单账户」就断言会得到 `false`，那是**没展开**、不是功能缺失。

### NAS 接口实测（v0.3.40，2026-11-05 00:52）

| 接口 | 结果 |
|---|---|
| `/health` | `{"status":"ok","version":"0.3.40","mode":"free"}` |
| `/api/data/version` | `{"version":"v0.3.40"}` |
| `/api/risk/timing` | `ok=True as_of=2026-11-04 stale=0 state=risk_off bars=120 backfilled=None` |
| `/api/regime/coverage` | `rows=244 latest=2026-11-04` |
| `/api/regime/latest` | `row.date=2026-11-04 state=strong score=83 phase=repair` |

> 注：`/api/regime/latest` 的数据在 **`row`** 子对象里，不是顶层扁平字段（探测脚本容易踩）。


| **v0.3.37 / 0.3.38 重建** | **71~75 s** | 只改 `backend/app/indicators/pipeline.py`(+`services/market_overview_builder.py`+`api/screener.py`)，pnpm install 与 apt 层全命中缓存 |
| **v0.3.39 重建** | **76 s** | 改 `main.py` + `services/market_timing.py` + `services/index_daily_free.py`，缓存全命中 |
| **v0.3.40 重建** | **76.8 s** | 只改 `main.py` + `market_timing.py` + `index_daily_free.py`，缓存全命中 |
| **v0.3.41 重建** | **约 80 s** | 改 `main.py` + `jobs/daily_pipeline.py` + `services/market_timing.py` + `services/index_daily_free.py` + 新增 `services/market_context_refresh.py` + 前端 `api.ts`/`RiskWidgets.tsx`，pnpm 层需重装（前端有改动） |
| **v0.3.43 重建** | **278 s** | 改 `strategy/paper_auto.py` + `api/paper.py` + 前端 `lib/api.ts`/`custom/paper/Page.tsx`/`holdingsColumns.test.tsx`，pnpm 层需重装；另单独跑 `_deploy/migrate_position_mode_0343.py` 迁移 21 个账户规则 |
| **v0.3.44 重建** | **267.5 s** | 纯后端修复：只改 `strategy/paper_auto.py`（+ 新测试文件）；前端无 src 改动，但镜像内从 `frontend/` 源码 pnpm build，故 `package.json` 仍要上传 |

踩到的两个坑（已在 compose 里解决）：

1. **bind mount 目录必须先存在** —— 群晖 docker 不会自动创建，`data/` 需提前 `mkdir`，否则报 `Bind mount failed: '/volume1/docker/TSP/data' does not exist`。
2. **镜像里的依赖在 uv 的 venv**（`/app/.venv`），不是系统 python。排查时要用
   `sudo docker exec TickFlow_Stock_Panel /app/.venv/bin/python -c "import polars"`。

另：启动日志里那行 `读 /frontend/package.json 失败，回退 _FALLBACK_VERSION=0.3.29` 是无害的 ——
容器内 `WORKDIR=/app`，而代码按开发布局推导项目根，找不到文件就回退到内置版本号，而内置版本号正好也是 0.3.29，显示正确。

第三个坑（v0.3.27 遇到）：**打印构建日志会崩**。`docker compose build` 的输出里含 `✓`，
在 GBK 控制台下 `print()` 直接抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`。
构建本身已成功，但脚本挂在打印那一步、没走到 `up -d`。解法：脚本开头
`sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")`，
或设 `$env:PYTHONIOENCODING="utf-8"`。
