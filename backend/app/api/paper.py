"""虚拟账户(模拟盘) API — 薄胶水: 校验/映射在 paper 域模块, 这里只做 HTTP 壳。

多账户: 所有端点接受 ?account=<id> (缺省 "default"); 帐户 ID 校验在域模块。

路由挂载在 app/main.py; 域模块自带进程内写锁, API 层不再加第二把锁
(镜像 lots API 的跨请求互斥语义, 但锁下沉到域)。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.strategy import paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])

# 批量操作上限: 防御超大批量把一次请求拖成分钟级 (前端多选也远超不到这个量)
MAX_BATCH_ACCOUNTS = 100
MAX_PLAN_ROWS = 100


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


#: 活跃计划状态 (参与 overview 持仓 join 的口径, 与 paper_plans 域保持一致)
_PLAN_JOIN_STATUSES = ("pending", "bought", "holding")


def _plan_source_label(plan: dict) -> str | None:
    """计划来源 → 人话标签: 计划本身没有 rule_name/strategy_id 字段, 按其
    source 归类 (strategy → 「策略来源」, 其余原样) 供前端直出。"""
    src = str(plan.get("source") or "").strip()
    if not src:
        return None
    if src == "strategy":
        return "策略来源"
    return src


def _join_holding_plans(data_dir: Path, account_id: str, holdings: list[dict]) -> None:
    """给 overview 的 holdings 行补计划字段 (缺失不写)。同一 symbol 多条活跃
    计划取 created_at 最早的一条 (list_plans 返回倒序, 故反向取最后一个)。
    join 失败只留 warning, 不影响 overview 返回。"""
    try:
        from app.strategy import paper_plans

        symbols = {h["symbol"] for h in holdings}
        plans = paper_plans.list_plans(data_dir, account_id, status=_PLAN_JOIN_STATUSES)
        earliest: dict[str, dict] = {}
        for plan in plans:
            sym = str(plan.get("symbol") or "")
            if sym in symbols:
                earliest[sym] = plan  # 倒序遍历, 后写的覆盖 → 最终剩最早一条
        for h in holdings:
            plan = earliest.get(h["symbol"])
            if plan is None:
                continue
            # 持仓/成交都没落名称时, 计划里的名称是最后的本地兜底
            if not h.get("name"):
                plan_name = str(plan.get("name") or "").strip()
                if plan_name:
                    h["name"] = plan_name
            stop = plan.get("stop")
            t1 = plan.get("target1")
            t2 = plan.get("target2")
            h["plan_stop_loss"] = float(stop) if stop is not None else None
            h["plan_target1"] = float(t1) if t1 is not None else None
            h["plan_target2"] = float(t2) if t2 is not None else None
            h["plan_entry_mode"] = str(plan.get("entry_mode")) if plan.get("entry_mode") is not None else None
            h["plan_source"] = _plan_source_label(plan)
            low = plan.get("entry_low")
            high = plan.get("entry_high")
            h["plan_entry_low"] = float(low) if low is not None else None
            h["plan_entry_high"] = float(high) if high is not None else None
    except Exception as e:  # noqa: BLE001 — 计划 join 属展示增强, 失败不阻断 overview
        logger.warning("paper overview holdings 计划 join 失败: %s", e)
        for h in holdings:
            h["plan_stop_loss"] = None
            h["plan_target1"] = None
            h["plan_target2"] = None
            h["plan_entry_mode"] = None
            h["plan_source"] = None
            h["plan_entry_low"] = None
            h["plan_entry_high"] = None


def _fill_holding_names(request: Request, holdings: list[dict]) -> None:
    """持仓行名称兜底: 台账里的 name 优先, 缺失时查维表, 再缺失查在线快照。

    维表曾被降级行情源冲成「只剩沪市 2823 行」, 那之后成交的深市持仓在
    positions.json 与 fills.jsonl 里都没有名称, 维表本身也查不到 —— 最后再用
    腾讯批快照按代码查一次 (带 10 分钟进程缓存)。三步都失败就保持 None,
    前端只显示代码, 不影响任何数值口径。
    """
    _fill_names_in_place(request, holdings)


def _fill_names_in_place(request: Request, rows: list[dict]) -> None:
    """就地回填 rows[].name (三步兜底), 持仓与交易计划共用同一套口径。

    0.3.46 之前自动跟单补建计划时没有把名称传下去, 那批存量计划 name 恒为
    None, 计划表「代码/名称」只能显示代码。这里与持仓完全同源:
    ①行内已有 name ②维表 get_name_map ③腾讯实时快照(带缓存)。
    任何一步失败都只留代码, 绝不影响数值字段。
    """
    missing = [r["symbol"] for r in rows if not r.get("name") and r.get("symbol")]
    if not missing:
        return
    repo = getattr(request.app.state, "repo", None)
    if repo is None or not hasattr(repo, "get_name_map"):
        # 无维表访问权 (裸 app 的单测 / 未初始化仓储) → 不做网络兜底,
        # 保证单测不因展示增强而依赖外网
        return
    try:
        name_map = repo.get_name_map(missing) or {}
    except Exception as e:  # noqa: BLE001 — 名称属展示增强, 失败不阻断
        logger.warning("名称兜底查询失败 (维表): %s", e)
        name_map = {}
    for r in rows:
        if not r.get("name"):
            nm = str(name_map.get(r.get("symbol") or "") or "").strip()
            if nm:
                r["name"] = nm

    still = [r["symbol"] for r in rows if not r.get("name") and r.get("symbol")]
    if not still:
        return
    try:
        from app.services.instrument_sync import lookup_names_online

        online = lookup_names_online(still) or {}
    except Exception as e:  # noqa: BLE001 — 在线兜底失败保持代码显示
        logger.warning("名称兜底查询失败 (在线): %s", e)
        return
    for r in rows:
        if not r.get("name"):
            nm = str(online.get(r.get("symbol") or "") or "").strip()
            if nm:
                r["name"] = nm


def _acc(request: Request, account: str) -> str:
    try:
        return paper.validate_account_id(account)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _migrate_queue_limit(data_dir: Path) -> None:
    """懒触发存量账户 queue_limit_orders 迁移 (函数内部有标记文件短路, 开销可忽略)。

    用户反馈涨停板不该买得进去 —— 新账户默认已开启排队, 存量账户必须同样修正。
    迁移失败不影响读接口本身 (软失败只告警)。
    """
    try:
        paper.migrate_queue_limit_defaults(data_dir)
    except Exception as e:  # noqa: BLE001 — 迁移问题不能阻断账户读取
        logger.warning("queue_limit_orders 迁移失败 (不影响本次读取): %s", e)


def _unique_account_ids(raw: list[str] | None) -> list[str]:
    """去重 + 校验批量上限 (空列表 400; id 本身非法留给后续逐个校验并计入 failed)。"""
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ids.append(s)
    if not ids:
        raise HTTPException(status_code=400, detail="account_ids 不能为空")
    if len(ids) > MAX_BATCH_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"一次最多操作 {MAX_BATCH_ACCOUNTS} 个账户")
    return ids


class AccountModel(BaseModel):
    initial_cash: float
    account_id: str = paper.DEFAULT_ACCOUNT_ID
    name: str | None = None
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float = paper.DEFAULT_STAMP_TAX_PCT
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = True


class OrderModel(BaseModel):
    symbol: str
    side: str                       # buy / sell
    qty: int | None = None
    amount: float | None = None
    order_type: str = "market"      # market / next_open / close
    ref_price: float | None = None  # amount 模式折算 & 买入资金预检


class SettingsModel(BaseModel):
    queue_limit_orders: bool | None = None
    commission_pct: float | None = None   # 佣金率 (0.00025 = 万2.5)
    stamp_tax_pct: float | None = None    # 印花税 (0.001 = 千1, 仅卖出)
    slippage_bps: float | None = None     # 滑点 (5 = 5bps)


class BatchAccountsModel(BaseModel):
    account_ids: list[str]
    force: bool = False                   # batch_delete: 连有持仓/待成交的账户一起删
    status: str | None = None             # batch_status: active / frozen


class ResetAccountModel(BaseModel):
    account: str | None = None            # 旧参数名
    account_id: str | None = None         # 别名 (新前端按 account_id 传)
    keep_rules: bool = True               # 保留 auto_rules 与 plan_config
    confirm: bool = False                 # 破坏性操作必须显式确认


def _resolve_asset_type(request: Request, symbol: str) -> str:
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


def _last_close(data_dir: Path, symbol: str, asset_type: str) -> float | None:
    """最近收盘价 (amount 折算参考价)。读最新分区日 raw close。"""
    base = paper._daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        import polars as pl
        df = (
            pl.scan_parquet((base / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol") == symbol)
            .sort("date")
            .select(pl.col("close").last())
            .collect()
        )
    except Exception:
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


@router.get("/accounts")
def list_accounts(request: Request):
    """账户列表 (带最新净值摘要, 供前端切换器)。"""
    data_dir = _data_dir(request)
    _migrate_queue_limit(data_dir)
    return {"accounts": paper.list_accounts(data_dir)}


@router.get("/account")
def get_account(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    _migrate_queue_limit(data_dir)
    acc = paper.get_account(data_dir, _acc(request, account))
    return {"account": acc}


@router.post("/account")
def create_account(request: Request, body: AccountModel):
    try:
        acc = paper.create_account(
            _data_dir(request),
            body.initial_cash,
            account_id=_acc(request, body.account_id),
            name=body.name,
            commission_pct=body.commission_pct,
            stamp_tax_pct=body.stamp_tax_pct,
            slippage_bps=body.slippage_bps,
            queue_limit_orders=body.queue_limit_orders,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


@router.delete("/account")
def delete_account(request: Request, force: bool = False, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """删除账户及其全部数据 (不可恢复)。仍有持仓或待成交订单时须 force=true。"""
    result, err = paper.delete_account(_data_dir(request), _acc(request, account), force=force)
    if err:
        raise HTTPException(status_code=400 if "force" in (err or "") else 404, detail=err)
    from app.strategy import paper_watch

    paper_watch.sync_from_state(request.app.state, _data_dir(request))  # 账户删了, 绑定集变了
    return {"ok": True, "deleted": _acc(request, account), **(result or {})}


@router.get("/overview")
def overview(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """总览: 现金 + 持仓估值 + 账户状态。

    估值口径: 盘中(交易时段且实时快照就绪)用当日实时 raw_close 估算
    (estimating=true); 其余时刻 (含盘前/收盘后/周末) 用最近一根日线收盘价,
    不再退回成本价 —— 否则非交易时段浮盈恒显示 0。
    """
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    _migrate_queue_limit(data_dir)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        return {"initialized": False, "account_id": acc_id}
    ov = paper.overview(data_dir, account_id=acc_id, valuation="daily")
    ov["fees"] = {k: acc[k] for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")}
    if ov.get("initialized") and ov.get("holdings"):
        _join_holding_plans(data_dir, acc_id, ov["holdings"])
        _fill_holding_names(request, ov["holdings"])

    # 盘中实时估算: 从行情服务的当日 enriched 快照取 raw_close (软失败, 降级日线)
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None and acc.get("status") == "active":
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today() and ov.get("holdings"):
                snapshot = dict(zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False))
                live = paper.overview(data_dir, snapshot, account_id=acc_id)
                live["fees"] = ov["fees"]
                if live.get("holdings"):
                    _join_holding_plans(data_dir, acc_id, live["holdings"])
                    _fill_holding_names(request, live["holdings"])
                return live
        except Exception as e:
            logger.warning("paper overview 实时估算失败, 降级日线口径: %s", e)
    return ov


@router.post("/orders")
def create_order(request: Request, body: OrderModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    asset_type = _resolve_asset_type(request, body.symbol)
    ref_price = body.ref_price
    if ref_price is None:
        ref_price = _last_close(data_dir, body.symbol, asset_type)
    order, err = paper.create_order(
        data_dir, body.symbol, body.side,
        account_id=acc_id,
        qty=body.qty, amount=body.amount,
        order_type=body.order_type, asset_type=asset_type, ref_price=ref_price,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/orders")
def list_orders(request: Request, status: str | None = None, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    orders = paper.load_orders(_data_dir(request), _acc(request, account))
    if status:
        orders = [o for o in orders if o["status"] == status]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return {"orders": orders}


@router.delete("/orders/{order_id}")
def cancel_order(request: Request, order_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    order, err = paper.cancel_order(_data_dir(request), order_id, _acc(request, account))
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"order": order}


@router.get("/trades")
def list_trades(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    fills = paper.load_fills(_data_dir(request), _acc(request, account))
    fills.sort(key=lambda f: f.get("seq", 0), reverse=True)
    return {"fills": fills}


@router.get("/positions")
def list_positions(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    # 非交易时段也要有浮盈: 用最近日线收盘价估值 (不再退回成本价)
    ov = paper.overview(data_dir, account_id=acc_id, valuation="daily")
    holdings = ov.get("holdings", [])
    if holdings:
        _join_holding_plans(data_dir, acc_id, holdings)
        _fill_holding_names(request, holdings)
    return {"holdings": holdings, "initialized": ov.get("initialized", False)}


@router.get("/nav")
def list_nav(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return {"nav": paper.load_nav(_data_dir(request), _acc(request, account))}


@router.get("/stats")
def get_stats(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return paper.stats(_data_dir(request), _acc(request, account))


@router.get("/rounds")
def list_rounds(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """FIFO 配对回合明细 (开平日期/数量/盈亏/持有天数), 供账户面板流水展示。"""
    return {"rounds": paper.round_trips(_data_dir(request), _acc(request, account))}


@router.get("/compare")
def compare_accounts(request: Request):
    """横向对比全部账户: 概览 + 回合统计 + 定版净值 (供对比表与净值叠加图)。

    净值给全量定版序列, 前端做归一化 (起点=1) 后多账户叠加。
    对比增量字段 (holdings_count/created_at/last_nav_date/day_change_pct/auto_rules)
    为加法扩展, 旧前端忽略不影响。
    """
    from app.strategy import paper_auto

    data_dir = _data_dir(request)

    # 盘中实时估算: 当日 enriched 快照就绪时取一次 raw_close 映射, 之后每账户
    # 纯内存 overview (与 /overview 实时口径一致); 失败降级定版口径
    live_snapshot: dict[str, float] | None = None
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None:
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today():
                live_snapshot = dict(
                    zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False)
                )
        except Exception as e:  # noqa: BLE001 — 快照来源不定 (行情服务/数据缺失), 软失败降级定版口径
            logger.warning("paper compare 实时快照不可用, 用定版口径: %s", e)

    rows = []
    for acc_id in paper.list_account_ids(data_dir):
        acc = paper.get_account(data_dir, acc_id)
        if acc is None:
            continue
        ov = paper.overview(data_dir, account_id=acc_id, valuation="daily")
        if not ov.get("initialized"):
            continue  # 空壳目录 (懒创建) 不进对比
        st = paper.stats(data_dir, acc_id)
        initial = float(acc.get("initial_cash") or 0)
        nav_rows = paper.load_nav(data_dir, acc_id)
        day_change_pct = None
        if len(nav_rows) >= 2 and float(nav_rows[-2]["nav"] or 0) > 0:
            day_change_pct = round((float(nav_rows[-1]["nav"]) / float(nav_rows[-2]["nav"]) - 1) * 100, 2)
        rules = paper_auto.load_auto_rules(data_dir, acc_id)
        # 盘中估算字段 (加法扩展): live_* 为快照价口径, 旧前端忽略不影响
        live_total = live_pnl_pct = None
        live_ov: dict | None = None
        if live_snapshot is not None and acc.get("status") == "active":
            live_ov = paper.overview(data_dir, live_snapshot, account_id=acc_id)
            if live_ov.get("initialized"):
                live_total = live_ov.get("total")
                if initial > 0:
                    live_pnl_pct = round((live_ov.get("total_pnl") or 0) / initial * 100, 2)
        rows.append({
            "live_total": live_total,
            "live_pnl_pct": live_pnl_pct,
            "estimating": live_total is not None,
            "account": acc_id,
            "name": acc.get("name") or acc_id,
            "status": acc.get("status"),
            "initial_cash": initial,
            "fees": {k: acc.get(k) for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")},
            "total": ov.get("total"),
            "cash": ov.get("cash"),
            "market_value": ov.get("market_value"),
            "total_pnl": ov.get("total_pnl"),
            "pnl_pct": round((ov.get("total_pnl") or 0) / initial * 100, 2) if initial > 0 else None,
            # 今日盈亏 (加法扩展字段): 优先「当前总资产 − 上一交易日定版净值」,
            # 无定版净值时退回持仓浮动日盈亏; 盘中 live_* 口径另有 day 值
            "day_pnl": (live_ov.get("day_pnl") if live_ov is not None else ov.get("day_pnl")),
            "day_pnl_pct": (live_ov.get("day_pnl_pct") if live_ov is not None else ov.get("day_pnl_pct")),
            "rounds": st.get("rounds"),
            "win_rate": st.get("win_rate"),
            "profit_loss_ratio": st.get("profit_loss_ratio"),
            "avg_holding_days": st.get("avg_holding_days"),
            "realized_pnl": st.get("realized_pnl"),
            "max_drawdown": st.get("max_drawdown"),
            "nav": [{"date": n["date"], "nav": n["nav"]} for n in nav_rows],
            # 对比展示用增量字段
            "holdings_count": len(ov.get("holdings") or []),
            "created_at": acc.get("created_at"),
            "last_nav_date": nav_rows[-1]["date"] if nav_rows else None,
            "day_change_pct": day_change_pct,
            "auto_rules": [
                {
                    "name": r.get("name"),
                    "match_kind": r.get("match_kind"),
                    "match_id": r.get("match_id"),
                    "side": r.get("side"),
                    "enabled": bool(r.get("enabled")),
                }
                for r in rules
            ],
            "auto_enabled": sum(1 for r in rules if r.get("enabled")),
        })
    return {"accounts": rows}


@router.post("/rebuild")
def rebuild(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """由成交台账重建物化持仓与现金 (修复兜底)。"""
    positions = paper.rebuild_positions(_data_dir(request), _acc(request, account))
    return {"symbols": len(positions)}


def _set_status(request: Request, acc_id: str, status: str) -> dict:
    """置账户状态 (active/frozen); 撮合/跟单/计划三条链路都会跳过非 active 账户。"""
    from app.strategy import paper_watch

    data_dir = _data_dir(request)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        raise HTTPException(status_code=400, detail="尚未创建模拟账户")
    with paper.PAPER_LOCK:
        acc["status"] = status
        paper.save_account(data_dir, acc, acc_id)
    paper_watch.sync_from_state(request.app.state, data_dir)  # 启停改变了参与监控的账户集
    return acc


@router.post("/freeze")
def freeze(request: Request, frozen: bool = True, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """冻结/解冻账户: 冻结后拒绝新订单、暂停撮合与跟单, 持仓只读。"""
    return {"account": _set_status(request, _acc(request, account), "frozen" if frozen else "active")}


@router.post("/accounts/{account_id}/start")
def start_account(request: Request, account_id: str):
    """启动账户 (= 解冻): 恢复撮合、自动跟单与计划触发。"""
    return {"ok": True, "account": _set_status(request, _acc(request, account_id), "active")}


@router.post("/accounts/{account_id}/stop")
def stop_account(request: Request, account_id: str):
    """停止账户 (= 冻结): 暂停撮合、自动跟单与计划触发, 不删任何数据。"""
    return {"ok": True, "account": _set_status(request, _acc(request, account_id), "frozen")}


@router.post("/accounts/batch_status")
def batch_status(request: Request, body: BatchAccountsModel):
    """批量启动/停止: 逐个生效, 单个失败不阻断其它账户。"""
    from app.strategy import paper_watch

    status = (body.status or "").strip()
    if status not in ("active", "frozen"):
        raise HTTPException(status_code=400, detail="status 只能是 active 或 frozen")
    ids = _unique_account_ids(body.account_ids)
    data_dir = _data_dir(request)
    done: list[dict] = []
    failed: list[dict] = []
    for raw in ids:
        try:
            acc_id = paper.validate_account_id(raw)
            acc = _set_status(request, acc_id, status)
            done.append({"account": acc_id, "status": acc.get("status"), "name": acc.get("name")})
        except HTTPException as e:
            failed.append({"account": raw, "error": str(e.detail)})
        except Exception as e:  # noqa: BLE001 — 单个账户失败不能中断整批
            logger.warning("paper 批量置状态失败 %s: %s", raw, e)
            failed.append({"account": raw, "error": str(e)})
    paper_watch.sync_from_state(request.app.state, data_dir)
    return {"ok": not failed, "updated": done, "failed": failed, "status": status}


@router.post("/accounts/batch_delete")
def batch_delete(request: Request, body: BatchAccountsModel):
    """批量删除账户: 逐个删除, 单个失败 (如仍有持仓且未 force) 不阻断整批。"""
    from app.strategy import paper_watch

    ids = _unique_account_ids(body.account_ids)
    data_dir = _data_dir(request)
    deleted: list[str] = []
    failed: list[dict] = []
    for raw in ids:
        try:
            acc_id = paper.validate_account_id(raw)
            _result, err = paper.delete_account(data_dir, acc_id, force=body.force)
            if err:
                failed.append({"account": acc_id, "error": err})
            else:
                deleted.append(acc_id)
        except ValueError as e:
            failed.append({"account": raw, "error": str(e)})
        except Exception as e:  # noqa: BLE001 — 单个账户失败不能中断整批
            logger.warning("paper 批量删除失败 %s: %s", raw, e)
            failed.append({"account": raw, "error": str(e)})
    paper_watch.sync_from_state(request.app.state, data_dir)  # 绑定集变了
    return {"ok": not failed, "deleted": deleted, "failed": failed}


@router.post("/reset")
def reset_account(request: Request, body: ResetAccountModel | None = None, confirm: bool = False,
                  account: str | None = None, keep_rules: bool = True):
    """重置账户: 清空订单/成交/持仓/净值/计划, 现金回初始资金, 状态回 active。

    破坏性操作, 必须 confirm=true (query 或 body 任一) 才执行; 默认保留自动跟单规则
    与计划参数 (keep_rules=false 一并清)。
    """
    from app.strategy import paper_watch

    body = body or ResetAccountModel()
    if not (confirm or body.confirm):
        raise HTTPException(status_code=400, detail="重置会清空该账户全部订单/成交/持仓/净值, 需 confirm=true 确认")
    raw = body.account_id or account or body.account or paper.DEFAULT_ACCOUNT_ID
    keep = bool(body.keep_rules and keep_rules)
    result, err = paper.reset_account(_data_dir(request), _acc(request, raw), keep_rules=keep)
    if err:
        raise HTTPException(status_code=404, detail=err)
    paper_watch.sync_from_state(request.app.state, _data_dir(request))
    acc = (result or {}).get("account") or {}
    return {"ok": True, "account": acc, "cleared": (result or {}).get("cleared") or {}}


@router.post("/settings")
def update_settings(request: Request, body: SettingsModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """更新账户设置 (涨跌停排队等开关)。"""
    try:
        acc = paper.update_settings(_data_dir(request), _acc(request, account), **body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


# ===== 自动跟单规则 (V2) =====
class AutoRuleModel(BaseModel):
    name: str
    match_kind: str                 # strategy / rule
    match_id: str
    side: str = "buy"
    size_mode: str = "fixed_amount" # fixed_amount / pct_equity
    size_value: float
    order_type: str = "next_open"
    cooldown_days: int = 5
    max_positions: int = 0             # 最大同时持仓标的数, 0 不限 (对齐回测口径)
    position_mode: str = "fixed"       # fixed=每票固定% / equal_total=总仓均分(建议总仓÷实际只数)
    enabled: bool = True


@router.get("/auto_rules")
def list_auto_rules(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    return {"rules": paper_auto.load_auto_rules(_data_dir(request), _acc(request, account))}


@router.post("/auto_rules")
def create_auto_rule(request: Request, body: AutoRuleModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    try:
        rule = paper_auto.create_auto_rule(_data_dir(request), body.model_dump(), _acc(request, account))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    paper_watch.sync_from_state(request.app.state, _data_dir(request))  # 绑定即评估
    return {"rule": rule}


@router.post("/auto_rules/{rule_id}/enabled")
def set_auto_rule_enabled(request: Request, rule_id: str, enabled: bool, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    rule = paper_auto.set_enabled(_data_dir(request), rule_id, enabled, _acc(request, account))
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    paper_watch.sync_from_state(request.app.state, _data_dir(request))
    return {"rule": rule}


@router.delete("/auto_rules/{rule_id}")
def delete_auto_rule(request: Request, rule_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    if not paper_auto.delete_auto_rule(_data_dir(request), rule_id, _acc(request, account)):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    paper_watch.sync_from_state(request.app.state, _data_dir(request))
    return {"ok": True}


# ===== 对比批量创建 (V3): 同规格多账户并行对比的构造入口 =====
class ArenaSourceModel(BaseModel):
    name: str | None = None              # 账户名 (缺省用 match_id)
    match_kind: str                      # strategy / rule (与 auto_rules 同口径)
    match_id: str
    side: str = "buy"
    size_mode: str = "pct_equity"        # 对比默认按权益百分比, 不同本金也可公平比
    size_value: float = 10.0
    order_type: str = "next_open"
    cooldown_days: int = 5
    max_positions: int = 0             # 最大同时持仓标的数, 0 不限 (对齐回测口径)
    position_mode: str = "fixed"       # fixed=每票固定% / equal_total=总仓均分(建议总仓÷实际只数)


class ArenaBatchModel(BaseModel):
    initial_cash: float
    sources: list[ArenaSourceModel]
    name_prefix: str = ""                # 账户名前缀 (如 "第一期-", 可空)
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float = paper.DEFAULT_STAMP_TAX_PCT
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = True


@router.post("/arena/batch_create")
def arena_batch_create(request: Request, body: ArenaBatchModel):
    """对比批量创建: 同本金/同费率一次性开 N 个账户, 各绑一条自动跟单规则。

    公平对比由构造保证 (同初始资金 + 同费用口径 + 同成交约束, 复用 paper 域
    的锁与校验)。先整体校验全部规则再落盘 — 任一来源非法即 400, 不留半创建
    状态; 账户 id 服务端生成 (arena_ + 随机后缀), 不接受调用方指定。
    """
    from app.strategy import paper_auto

    if not body.sources:
        raise HTTPException(status_code=400, detail="至少需要一个来源")
    if len(body.sources) > 20:
        raise HTTPException(status_code=400, detail="单次最多创建 20 个对比账户")

    data_dir = _data_dir(request)
    try:
        prepared = []
        for src in body.sources:
            rule = {
                "name": f"对比·{(src.name or '').strip() or src.match_id}",
                "match_kind": src.match_kind,
                "match_id": (src.match_id or "").strip(),
                "side": src.side,
                "size_mode": src.size_mode,
                "size_value": src.size_value,
                "order_type": src.order_type,
                "cooldown_days": src.cooldown_days,
                "max_positions": src.max_positions,
                "position_mode": src.position_mode,
                "enabled": True,
            }
            paper_auto.validate_rule(rule)  # 全部通过前不落盘 (fail-closed)
            suffix = (src.name or "").strip() or src.match_id
            prefix = (body.name_prefix or "").strip()
            acc_name = f"{prefix}{suffix}" if prefix else suffix
            prepared.append((rule, acc_name))

        created = []
        for rule, acc_name in prepared:
            account_id = f"arena_{uuid.uuid4().hex[:10]}"
            paper.create_account(
                data_dir, body.initial_cash,
                account_id=paper.validate_account_id(account_id),
                name=acc_name,
                commission_pct=body.commission_pct,
                stamp_tax_pct=body.stamp_tax_pct,
                slippage_bps=body.slippage_bps,
                queue_limit_orders=body.queue_limit_orders,
            )
            saved = paper_auto.create_auto_rule(data_dir, rule, account_id=account_id)
            created.append({"account": account_id, "name": acc_name, "rule_id": saved["id"]})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    from app.strategy import paper_watch

    paper_watch.sync_from_state(request.app.state, data_dir)  # 绑定即评估
    return {"created": created}


# ===== 交易计划 (plan): 买入区间/止损/止盈/仓位随大盘情绪缩放 =====
# 说明: 老接口的参数名是 ?account=(V2 历史), 计划接口按前端契约用 ?account_id=,
# 这里两者都接受 (account_id 优先), 避免新旧调用方各自踩坑。
def _plan_acc(request: Request, account_id: str | None, account: str | None) -> str:
    return _acc(request, account_id or account or paper.DEFAULT_ACCOUNT_ID)


class PlanModel(BaseModel):
    account_id: str | None = None
    symbol: str
    name: str | None = None
    entry_low: float                      # 买入区间下沿
    entry_high: float                     # 买入区间上沿
    entry_ref: float | None = None        # 区间中枢 (缺省取 (low+high)/2)
    stop: float | None = None             # 止损价 (缺省不设, 只做止盈)
    target1: float | None = None          # 目标1 (触及减半)
    target2: float | None = None          # 目标2 (触及清仓)
    pos_pct: float | None = None          # 仓位占比%, 与 qty_planned 二选一
    qty_planned: int | None = None        # 计划股数 (100 整数倍)
    entry_mode: str | None = None         # zone(进区间才买) / next_open / market
    source: str | None = None             # manual / strategy / ...
    note: str | None = None


class PlanFromStrategyModel(BaseModel):
    account_id: str | None = None
    strategy_id: str
    params: dict | None = None
    top_n: int | None = None             # 优中选优截断 (缺省用 cfg.top_n)
    rank_key: str = "score"              # 按该列降序取前 N (缺失该列保持原顺序)
    rank_desc: bool = True
    cfg: dict | None = None               # 本次覆盖计划参数 (不落盘, 用 PUT /plan_config 持久化)
    entry_mode: str | None = None         # zone / next_open / market (缺省用 cfg)
    asset_type: str = "stock"
    timeframe: str = "1d"


class PlanConfigModel(BaseModel):
    account_id: str | None = None
    cfg: dict | None = None


class PlanFromRowsModel(BaseModel):
    account_id: str | None = None
    rows: list[dict] = []                 # 前端勾选的结果行原样回传 (symbol 必有)
    cfg: dict | None = None               # 本次覆盖计划参数 (不落盘)
    top_n: int | None = None             # 优中选优截断 (缺省用 cfg.top_n)
    rank_key: str = "score"              # 按该列降序取前 N
    rank_desc: bool = True
    entry_mode: str | None = None         # zone / next_open / market (缺省用 cfg)
    source: str = "screener"


def _check_entry_mode(entry_mode: str | None, cfg: dict) -> str | None:
    """校验请求里的 entry_mode: 未传返回 None (交给域模块用 cfg 默认值), 非法 → 400。"""
    if entry_mode is None or (isinstance(entry_mode, str) and not entry_mode.strip()):
        return None
    from app.strategy import trade_plan

    mode = trade_plan.normalize_entry_mode(entry_mode, default=None)
    if mode is None:
        raise HTTPException(
            status_code=400,
            detail=f"entry_mode 只能是 {'/'.join(trade_plan.ENTRY_MODES)}, 收到 {entry_mode!r}",
        )
    return mode


def _check_top_n(top_n: int | None) -> int | None:
    """校验优中选优的截断数量: 未传返回 None (交给 cfg.top_n), 越界 → 400。"""
    if top_n is None:
        return None
    try:
        value = int(top_n)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"top_n 必须是整数, 收到 {top_n!r}") from e
    if value < 1 or value > MAX_PLAN_ROWS:
        raise HTTPException(
            status_code=400, detail=f"top_n 必须在 1~{MAX_PLAN_ROWS} 之间, 收到 {value}"
        )
    return value


def _check_rank_key(rank_key: str | None) -> str:
    """校验排序用的列名: 空值 → 400 (避免按空列名「排序」后顺序漂移)。"""
    key = (rank_key or "").strip() if isinstance(rank_key, str) else ""
    if not key:
        raise HTTPException(status_code=400, detail="rank_key 不能为空")
    return key


@router.get("/plans")
def list_plans(
    request: Request,
    status: str | None = None,
    account_id: str | None = None,
    account: str | None = None,
):
    """账户交易计划列表 (status 支持逗号分隔多状态过滤), 按创建时间倒序。"""
    from app.strategy import paper_plans

    plans = paper_plans.list_plans(
        _data_dir(request), _plan_acc(request, account_id, account), status=status
    )
    # 0.3.47: 存量自动计划的 name 多为 None (补建时没传), 计划表只剩代码;
    # 与持仓走同一套三步兜底 (行内 → 维表 → 腾讯快照), 纯展示、不改数值。
    _fill_names_in_place(request, plans)
    return {"plans": plans, "total": len(plans)}


@router.post("/plans")
def create_plan(
    request: Request,
    body: PlanModel,
    account_id: str | None = None,
    account: str | None = None,
):
    """新建手工交易计划 (前端「加一条计划」)。"""
    from app.strategy import paper_plans

    acc_id = _plan_acc(request, body.account_id or account_id, account)
    payload = body.model_dump()
    payload.pop("account_id", None)
    try:
        plan = paper_plans.create_plan(_data_dir(request), payload, acc_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "plan": plan}


@router.post("/plans/from_strategy")
def create_plans_from_strategy(
    request: Request,
    body: PlanFromStrategyModel,
    account_id: str | None = None,
    account: str | None = None,
):
    """跑一次策略, 用结果行批量生成交易计划 (区间/止损/仓位按大盘情绪缩放)。

    复用 /api/strategy/run 的取数链路 (ScreenerService + StrategyEngine), 差异只在
    产出: 那边返回明细行, 这边把行翻成可执行计划落盘。atr_14 列缺失时按 3% 波动率
    兜底 (zdk 策略没有 atr 列), 所以不需要策略返回固定列集。
    """
    from app.strategy import paper_plans, trade_plan

    acc_id = _plan_acc(request, body.account_id or account_id, account)
    data_dir = _data_dir(request)
    engine = getattr(request.app.state, "strategy_engine", None) or getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")

    from app.services.screener import ScreenerService

    svc = ScreenerService(request.app.state.repo, asset_type=body.asset_type)
    as_of = svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")
    params = dict(body.params or {})
    overrides: dict = {}
    try:
        from app.strategy import config as strategy_config

        overrides = strategy_config.load_override(data_dir, body.strategy_id) or {}
        if overrides.get("params"):
            merged = dict(overrides["params"])
            merged.update(params)   # 请求里的优先
            params = merged
    except Exception as e:  # noqa: BLE001 — 覆盖配置读不到不影响按默认参数跑
        logger.warning("paper plan 读取策略覆盖配置失败, 按请求参数执行: %s", e)
    try:
        context = svc.build_strategy_context(
            engine,
            as_of,
            [body.strategy_id],
            timeframe=body.timeframe,
            params_map={body.strategy_id: params},
            overrides_map={body.strategy_id: overrides},
        )
        result = engine.run(
            body.strategy_id,
            context,
            params=params,
            overrides=overrides or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    cfg = trade_plan.merge_cfg({**paper_plans.load_cfg(data_dir, acc_id), **(body.cfg or {})})
    mode = _check_entry_mode(body.entry_mode, cfg)
    top_n = _check_top_n(body.top_n)
    rank_key = _check_rank_key(body.rank_key)
    try:
        created, state = paper_plans.create_plans_from_rows(
            data_dir,
            list(getattr(result, "rows", None) or []),
            account_id=acc_id,
            cfg=cfg,
            top_n=top_n,
            entry_mode=mode,
            rank_key=rank_key,
            rank_desc=body.rank_desc,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    holdings = dict(getattr(created, "holdings", None) or {})
    portfolio = dict(getattr(created, "portfolio", None) or {})
    return {
        "ok": True,
        "created": len(created),
        "plans": created,
        "skipped": list(getattr(created, "skipped", None) or []),
        "state": state,
        "entry_mode": mode or cfg["default_entry_mode"],
        "as_of": str(getattr(result, "as_of", "") or ""),
        "rows": len(getattr(result, "rows", None) or []),
        "holdings_limit": holdings.get("limit"),
        "timing_exposure": holdings.get("timing_exposure"),
        "phase_exposure": holdings.get("phase_exposure"),
        "total_pos_pct": portfolio.get("total_pos_pct"),
        "holdings": holdings,
        "portfolio": portfolio,
    }


@router.delete("/plans/{plan_id}")
def delete_plan(
    request: Request,
    plan_id: str,
    account_id: str | None = None,
    account: str | None = None,
):
    """删除计划 (物理删除)。"""
    from app.strategy import paper_plans

    ok = paper_plans.delete_plan(_data_dir(request), plan_id, _plan_acc(request, account_id, account))
    if not ok:
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    return {"ok": True}


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(
    request: Request,
    plan_id: str,
    account_id: str | None = None,
    account: str | None = None,
):
    """取消计划 (留档, 状态置 cancelled; 已了结的计划原样返回)。"""
    from app.strategy import paper_plans

    plan = paper_plans.cancel_plan(_data_dir(request), plan_id, _plan_acc(request, account_id, account))
    if plan is None:
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    return {"ok": True, "plan": plan}


@router.get("/plan_config")
def get_plan_config(
    request: Request,
    account_id: str | None = None,
    account: str | None = None,
):
    """计划参数 + 当前大盘情绪 + 面板文案 (state→中文 / 仓位倍率表)。"""
    from app.strategy import paper_plans, trade_plan

    data_dir = _data_dir(request)
    acc_id = _plan_acc(request, account_id, account)
    cfg = paper_plans.load_cfg(data_dir, acc_id)
    state = trade_plan.latest_state(data_dir)
    timing = trade_plan.trend_exposure(data_dir)
    return {
        "cfg": cfg,
        "labels": paper_plans.labels(),
        "state": state,
        # 只数上限缩放口径 (前端展示「只数 5 → 本期最多 2 只」)
        "holdings": trade_plan.effective_holdings_limit(
            cfg, state, timing_exposure=timing.get("exposure")
        ),
        "timing": timing,
    }


@router.put("/plan_config")
def update_plan_config(
    request: Request,
    body: PlanConfigModel,
    account_id: str | None = None,
    account: str | None = None,
):
    """更新计划参数 (越界值回落默认, 未知键丢弃)。"""
    from app.strategy import paper_plans

    acc_id = _plan_acc(request, body.account_id or account_id, account)
    cfg = paper_plans.save_cfg(_data_dir(request), body.cfg, acc_id)
    return {"ok": True, "cfg": cfg}


@router.post("/plans/from_rows")
def create_plans_from_rows(
    request: Request,
    body: PlanFromRowsModel,
    account_id: str | None = None,
    account: str | None = None,
):
    """用前端已选好的结果行批量生成计划 — 不重跑策略。

    与 /plans/from_strategy 的本质区别: 那边由后端跑策略取 top_n, 用户无从挑选;
    这边把结果表里勾选的行原样传回来, 用户「挑几只」再决定买或不买。
    rows 只需 symbol 可读; name/close/low/atr_14 有则用, 缺了由 build_plan 兜底
    (atr 缺失按 3% 波动率估)。skipped 回报没建成的标的与原因。
    """
    from app.strategy import paper_plans, trade_plan

    acc_id = _plan_acc(request, body.account_id or account_id, account)
    data_dir = _data_dir(request)
    rows = list(body.rows or [])
    if not rows:
        raise HTTPException(status_code=400, detail="未选择任何标的")
    if len(rows) > MAX_PLAN_ROWS:
        raise HTTPException(status_code=400, detail=f"一次最多生成 {MAX_PLAN_ROWS} 条计划")
    cfg = trade_plan.merge_cfg({**paper_plans.load_cfg(data_dir, acc_id), **(body.cfg or {})})
    mode = _check_entry_mode(body.entry_mode, cfg)
    top_n = _check_top_n(body.top_n)
    rank_key = _check_rank_key(body.rank_key)
    try:
        created, state = paper_plans.create_plans_from_rows(
            data_dir,
            rows,
            account_id=acc_id,
            cfg=cfg,
            top_n=top_n if top_n is not None else len(rows),
            entry_mode=mode,
            rank_key=rank_key,
            rank_desc=body.rank_desc,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    holdings = dict(getattr(created, "holdings", None) or {})
    portfolio = dict(getattr(created, "portfolio", None) or {})
    return {
        "ok": True,
        "created": len(created),
        "plans": created,
        "skipped": list(getattr(created, "skipped", None) or []),
        "state": state,
        "entry_mode": mode or cfg["default_entry_mode"],
        "source": body.source or "screener",
        "holdings_limit": holdings.get("limit"),
        "timing_exposure": holdings.get("timing_exposure"),
        "phase_exposure": holdings.get("phase_exposure"),
        "total_pos_pct": portfolio.get("total_pos_pct"),
        "holdings": holdings,
        "portfolio": portfolio,
    }


@router.post("/plans/preview")
def preview_plans_from_rows(
    request: Request,
    body: PlanFromRowsModel,
    account_id: str | None = None,
    account: str | None = None,
):
    """只算不落盘: 把勾选的行变成「这只会怎么买 / 为什么买不了」。

    与 /plans/from_rows 的区别: 这个接口**不写任何计划**, 只返回每一行的预览:
      - 能做的 → 买入区间 / 止损 / 目标 / 仓位 / 股数 / 预期盈亏;
      - 做不了的 → `ok=false` + 中文原因 (如「计划股数不足一手, 一手需要 xx 元」),
        并尽量把能算的区间和止损一并返回, 方便界面解释而不是画一堆 `--`。

    前端据此展示, 用户看懂之后再决定要不要真的建计划。
    """
    from app.strategy import paper_plans, trade_plan

    acc_id = _plan_acc(request, body.account_id or account_id, account)
    data_dir = _data_dir(request)
    rows = list(body.rows or [])
    if len(rows) > MAX_PLAN_ROWS:
        raise HTTPException(status_code=400, detail=f"一次最多预览 {MAX_PLAN_ROWS} 条")
    cfg = trade_plan.merge_cfg({**paper_plans.load_cfg(data_dir, acc_id), **(body.cfg or {})})
    mode = _check_entry_mode(body.entry_mode, cfg)
    state = trade_plan.latest_state(data_dir)
    timing = trade_plan.trend_exposure(data_dir)

    overview = paper.overview(data_dir, account_id=acc_id) or {}
    equity = float(overview.get("total") or 0.0)
    previews = trade_plan.preview_plans_for_rows(
        rows,
        equity=equity,
        cfg=cfg,
        state=state,
        entry_mode=mode,
    )
    holdings = trade_plan.effective_holdings_limit(
        cfg, state, timing_exposure=timing.get("exposure")
    )
    ok_rows = [p for p in previews if p.get("ok")]
    return {
        "ok": True,
        "previews": previews,
        "account_id": acc_id,
        "equity": equity,
        "cash": overview.get("cash"),
        "market_value": overview.get("market_value"),
        "initial_cash": overview.get("initial_cash"),
        "account_name": overview.get("account_name"),
        "state": state,
        "entry_mode": mode or cfg["default_entry_mode"],
        "timing": timing,
        "holdings": holdings,
        "summary": {
            "total": len(previews),
            "ok": len(ok_rows),
            "blocked": len(previews) - len(ok_rows),
            "total_pos_pct": round(sum(float(p.get("pos_pct") or 0) for p in ok_rows), 2),
            "need_cash": round(
                sum(
                    float(p.get("qty_planned") or 0) * float(p.get("entry_ref") or 0)
                    for p in ok_rows
                ),
                2,
            ),
        },
    }

