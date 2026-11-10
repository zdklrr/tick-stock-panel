"""风险雷达 + 盘前简报 + 大盘择时 HTTP 接口 (薄壳, 装配逻辑在 app/services/*)。

单位口径 (前端按此渲染):
- `chg_pct` / `ma20_pct` / `pct_vs_ma20` / `pct_vs_ma60` / `gap_pct.*` / `amplitude` /
  `drawdown_pct` 都是**百分数数值** (-0.83 表示 -0.83%, 不是 -0.0083)。
- `score` 是 0~100 分 (50=中性, 越高越危险); `exposure` 是仓位系数 (0.35~1.10)。
- `breadth.dn_ratio` 是 0~1 小数 (0.72 = 72% 在跌)。
- `probs.{up,flat,down}` 是 **0~100** 的百分数, 三者相加为 100。
- `signal` 是合成信号原值 (例如 +0.42), 不是百分比。
- `suggested_exposure` 是仓位系数 (0.15~1.20); `suggested_exposure_pct` 是**百分数数值**
  (55.0 表示建议 55% 仓位), `parts[x].value` 同为仓位系数 (1.0 = 不加不减)。

数据日期口径 (与抓取时刻严格区分):
- `as_of` 一律是 `"YYYY-MM-DD"` 字符串或 `None`, 表示**数据本身属于哪一天**。
- `fetched_at` 是**什么时候抓的** (`"%Y-%m-%d %H:%M:%S"`)。两者不能互相顶替:
  美股隔夜数据在 A 股当天上午被抓到时, `fetched_at` 是今天而 `as_of` 是昨天。
- `/radar`: 顶层 `as_of` = 所有**可用且未过期**指数里最新的数据日期, 都不用时为
  `None`; `stale_days` = 该日期距今天数 (前端据此显示「滞后 N 天」)。
- `/premarket`: 顶层 `as_of` = **今天** (简报问的是「今天开门怎么开」);
  `us.as_of` / `asia.as_of` 是各自区域里有效指数的最新数据日期,
  `a50.as_of` / `cnh.as_of` 是今天 (期货/外汇为实时报价)。
- 任何取数失败都降级成 `as_of: None`, 不抛异常。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from app.services import global_risk, market_timing, premarket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/risk", tags=["risk"])


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/radar")
def risk_radar(
    request: Request,
    account: str = Query("default", max_length=32),
    refresh: bool = Query(False),
):
    """风险雷达快照: 7 个环球指数的分数 + A 股宽度 + 账户回撤 → 仓位系数。

    `refresh=true` 强制绕过 45 秒缓存重新联网 (设置页/手动刷新用)。
    """
    if refresh:
        global_risk.invalidate_cache()
    return global_risk.radar_snapshot(_data_dir(request), account_id=account, use_cache=not refresh)


@router.get("/premarket")
def risk_premarket(request: Request, refresh: bool = Query(False)):
    """盘前简报: A50 / 美股 / 亚太合成 → 开盘方向、缺口区间与三段概率。

    `refresh=true` 强制绕过 120 秒缓存。
    """
    if refresh:
        premarket.invalidate_cache()
    return premarket.premarket_snapshot(use_cache=not refresh)


@router.get("/timing")
def risk_timing(request: Request, symbol: str = Query(market_timing.DEFAULT_SYMBOL, max_length=16)):
    """大盘择时: 沪深300 收盘 vs MA20/MA60 → 四态 + 系数 + 建议。

    **只读本地日K, 不联网**: 本地缺 510300/000300.SH 时返回 ok=False 的 unknown,
    前端显示「数据缺失」而不是让页面等网络超时。
    """
    return market_timing.trend_timing(_data_dir(request), symbol=symbol)


@router.get("/advice")
def risk_advice(
    request: Request,
    account: str = Query("default", max_length=32),
):
    """综合建议仓位: 大盘择时 × 情绪周期 × 外围风险 → 可执行仓位数字。

    三路系数分别返回在 `parts` 里 (谁在压仓位一目了然); 任何一路缺失按中性 1.0
    参与并标 `missing=True`, 合成值夹在 [0.15, 1.20] 内**永不归零**。
    """
    return market_timing.synthesize(_data_dir(request), account_id=account)


@router.post("/refresh")
def risk_refresh(request: Request):
    """清空两层缓存 (前端「重试」按钮用)。"""
    global_risk.invalidate_cache()
    premarket.invalidate_cache()
    return {"ok": True, "msg": "风险雷达与盘前简报缓存已清空"}


@router.get("/health")
def risk_health(request: Request):
    """轻量自检: 不做网络请求, 只报告配置与缓存状况。"""
    return {
        "ok": True,
        "cache_ttl": global_risk.CACHE_TTL,
        "premarket_ttl": premarket._TTL,
        "stale_days": global_risk.STALE_DAYS,
        "global_indexes": [i["code"] for i in global_risk.GLOBAL_INDEXES],
        "region_weight": global_risk.REGION_WEIGHT,
        "exposure_range": [global_risk.EXPOSURE_FLOOR, global_risk.EXPOSURE_CAP],
        "dd_steps": [list(s) for s in global_risk.DD_STEPS],
        "dd_window_days": global_risk.DD_WINDOW_DAYS,
        "cache": global_risk.cache_stats(),
    }
