"""盘前把「大盘择时 / 情绪周期 / 风险雷达」刷到最新 (v0.3.41)。

背景: 用户要求「这些数据应该在每天开盘前就取到, 基本上 9:25 竞价结束就该是最新,
这是最迟」。v0.3.40 之前这三张卡只有两条更新路径, 都不保证盘前就绪:

1. 盘后 15:35 的 daily_pipeline —— 桌面端默认不常驻跑 (`pipeline_regime_enabled`
   默认 False), 笔记本晚上关机就完全不会执行;
2. 用户打开页面时的惰性自愈 (`market_timing.trend_timing` 的 `_heal` 分支) ——
   只有「有人来看」才触发, 且要等一次网络回补 + 重算, 首屏先看到的是旧数字。

于是 9:25 打开页面看到的是昨天的择时与情绪周期 (用户两次报「不是今天的数据」)。
本模块提供一次**幂等的盘前刷新**, 由调度器在工作日 09:20 (竞价结束前) 执行:

1. `ensure_timing_index_fresh`: 000300/510300 日K 落后就用免费源 (腾讯) 补;
2. `compute_regime_incremental` / `compute_mainline_incremental`: 补 enriched 里
   已有、而 regime/主线缺失或被覆写的交易日;
3. 清空风险雷达与盘前简报的记忆缓存, 让下一次读取重新联网取最新外围行情。

设计约束:
- **永不抛**: 盘前刷新挂在开盘前, 任何一路失败都只打日志, 绝不能挡住开盘;
- **幂等**: 各步都是「缺什么补什么」, 重复执行不会重复写盘;
- **只补择时链路需要的标的**, 全量指数属 TickFlow/自定义源的正路职责。
"""

from __future__ import annotations

import logging
from datetime import time as dt_time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 盘前刷新的时刻 (工作日)。竞价 09:15-09:25, 留 5 分钟给回补与重算,
#: 保证 09:25 竞价结束时页面读到的是最新状态 (用户明确的「最迟」时间点)。
PREMARKET_REFRESH_AT = dt_time(9, 20)

#: 盘前补的全量日历/主线口径 (与 daily_pipeline 的 Step 2.2/2.3 一致)。
MAINLINE_KINDS: tuple[str, ...] = ("concept", "industry")


def _resolve_data_dir(data_dir: Path | None) -> Path | None:
    """None → settings.data_dir; 配置不可用时返 None (调用方软失败)。"""
    if data_dir is not None:
        return Path(data_dir)
    try:
        from app.config import settings

        return Path(settings.data_dir)
    except Exception as e:  # noqa: BLE001 — 配置不可用按无数据目录处理
        logger.warning("盘前刷新读取配置失败: %s", e)
        return None


def refresh_market_context(data_dir: Path | None = None) -> dict:
    """盘前刷新三张卡的数据源。返回各步结果, 永不抛。

    返回结构::

        {"ok": bool,
         "timing": {"ok": bool, "fetched": {symbol: rows}, "reason": str},
         "regime": {"rows": int},
         "mainline": {"concept": int, "industry": int},
         "cache": {"cleared": list[str]},
         "errors": [str, ...]}
    """
    root = _resolve_data_dir(data_dir)
    result: dict = {
        "ok": True,
        "timing": {},
        "regime": {"rows": 0},
        "mainline": {},
        "cache": {"cleared": []},
        "errors": [],
    }
    if root is None:
        result["ok"] = False
        result["errors"].append("data_dir 不可用")
        return result

    # 1) 指数/ETF 日K 兜底回补 (大盘择时的原料)
    try:
        from app.services.index_daily_free import ensure_timing_index_fresh

        result["timing"] = ensure_timing_index_fresh(root)
        if not result["timing"].get("ok"):
            result["errors"].append(f"timing: {result['timing'].get('reason')}")
    except Exception as e:  # noqa: BLE001 — 盘前刷新永不抛
        logger.warning("盘前刷新 timing 失败(软): %s", e)
        result["errors"].append(f"timing: {e}")

    # 2) 情绪周期 + 市场主线增量补算 (依赖 enriched, 与上面回补互不牵连)
    repo = None
    try:
        from app.tickflow.repository import DataStore, KlineRepository

        repo = KlineRepository(DataStore(root))
    except Exception as e:  # noqa: BLE001
        logger.warning("盘前刷新打开仓库失败(软): %s", e)
        result["errors"].append(f"repo: {e}")

    if repo is not None:
        try:
            from app.api.regime import invalidate_regime_cache
            from app.services import regime_builder

            rows = regime_builder.compute_regime_incremental(repo, root)
            days = rows.height if not rows.is_empty() else 0
            result["regime"] = {"rows": days}
            if days:
                invalidate_regime_cache()
                logger.info("盘前刷新: 情绪周期补算 %d 天", days)
        except Exception as e:  # noqa: BLE001
            logger.warning("盘前刷新 情绪周期失败(软): %s", e)
            result["errors"].append(f"regime: {e}")

        for kind in MAINLINE_KINDS:
            try:
                from app.services import market_mainline

                rows = market_mainline.compute_mainline_incremental(repo, root, kind=kind)
                n = rows.height if not rows.is_empty() else 0
                result["mainline"][kind] = n
                if n:
                    logger.info("盘前刷新: 主线(%s)补算 %d 行", kind, n)
            except Exception as e:  # noqa: BLE001
                logger.warning("盘前刷新 主线(%s)失败(软): %s", kind, e)
                result["errors"].append(f"mainline/{kind}: {e}")

    # 3) 清掉风险雷达/盘前简报的记忆缓存, 下次读取重新联网取最新外围行情
    for name, module in (
        ("global_risk", "app.services.global_risk"),
        ("premarket", "app.services.premarket"),
    ):
        try:
            mod = __import__(module, fromlist=["invalidate_cache"])
            mod.invalidate_cache()
            result["cache"]["cleared"].append(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("盘前刷新清缓存(%s)失败(软): %s", name, e)
            result["errors"].append(f"cache/{name}: {e}")

    result["ok"] = not result["errors"]
    return result


__all__ = ["MAINLINE_KINDS", "PREMARKET_REFRESH_AT", "refresh_market_context"]
