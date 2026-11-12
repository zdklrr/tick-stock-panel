"""实盘扩展的 HTTP 接口 (挂载点 /api/custom/live-trader)。

与 paper_execution 同构: 通过扩展路由注册 + lifespan 接入, 不改主应用。
后台线程只在 `enabled=True` 时扫单, 默认不启动任何发单行为。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .broker import BrokerError
from .service import DEFAULT_CONFIG, LiveService

logger = logging.getLogger(__name__)


class ConfigRequest(BaseModel):
    """部分更新: 只传要改的字段。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    dry_run: bool | None = None
    exe_path: str | None = Field(default=None, max_length=500)
    accounts: list[str] | None = None
    sources: list[str] | None = None
    allow_sell: bool | None = None
    max_orders_per_day: int | None = Field(default=None, ge=0, le=200)
    max_order_amount: float | None = Field(default=None, gt=0, le=10_000_000)
    poll_seconds: int | None = Field(default=None, ge=2, le=300)


def build_router() -> APIRouter:
    service: LiveService | None = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service
        thread = None
        lease = None
        try:
            repo = getattr(app.state, "repo", None)
            if repo is None:
                raise ValueError("数据仓库未就绪")
            data_dir = Path(repo.store.data_dir)
            root = data_dir / "user_data" / "extensions" / "live_trader"
            root.mkdir(parents=True, exist_ok=True)
            # 单实例租约: 两个进程同时驱动同一个券商客户端会互相抢焦点
            lease = sqlite3.connect(root / "runner.sqlite", timeout=0)
            lease.execute("BEGIN EXCLUSIVE")
            service = LiveService(data_dir)
            active = service

            def run():
                while not active.stopping.is_set():
                    try:
                        if active.get_config().get("enabled"):
                            active.mirror_once()
                    except Exception:
                        logger.exception("live trader worker failed")
                    active.stopping.wait(int(active.get_config().get("poll_seconds") or 5))

            thread = threading.Thread(target=run, name="live-trader", daemon=True)
            thread.start()
            logger.info(
                "live trader extension ready (enabled=%s dry_run=%s)",
                service.get_config().get("enabled"),
                service.get_config().get("dry_run"),
            )
        except Exception:
            logger.exception("live trader extension unavailable")
            service = None
        try:
            yield
        finally:
            if service:
                service.stopping.set()
                try:
                    service.broker.stop()
                except Exception:
                    pass
            if thread:
                thread.join(timeout=3)
            if lease:
                lease.close()
            service = None

    router = APIRouter(
        prefix="/api/custom/live-trader", tags=["custom-live-trader"], lifespan=lifespan
    )

    def require_service() -> LiveService:
        if service is None:
            raise HTTPException(503, "实盘扩展未就绪或已有其他进程在驱动客户端, 请检查后端日志")
        return service

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BrokerError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, TypeError, KeyError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "实盘扩展数据无法读取或保存, 请检查数据目录和配置"
            )
            raise HTTPException(409, message) from exc

    @router.get("/status")
    def get_status():
        """配置 + 客户端连接状态 + 当日额度 (不触发 GUI)。"""
        return call(require_service().snapshot)

    @router.get("/schema")
    def get_schema():
        """默认配置, 供前端渲染表单。"""
        return {"defaults": dict(DEFAULT_CONFIG)}

    @router.post("/config")
    def post_config(body: ConfigRequest):
        patch = body.model_dump(exclude_none=True)
        return {"config": call(require_service().update_config, patch)}

    @router.post("/connect")
    def post_connect():
        svc = require_service()
        call(svc.connect)
        return {"broker": svc.broker.status()}

    @router.post("/disconnect")
    def post_disconnect():
        return {"broker": call(require_service().disconnect)}

    @router.get("/account")
    def get_account():
        """真实资金 + 持仓 (只读现读客户端)。"""
        return call(require_service().account_info)

    @router.get("/orders")
    def get_orders(limit: int = 100):
        return {"orders": call(require_service().recent_orders, limit)}

    @router.get("/entrusts")
    def get_entrusts():
        return {"entrusts": call(require_service().live_entrusts)}

    @router.get("/trades")
    def get_trades():
        return {"trades": call(require_service().live_trades)}

    @router.post("/mirror")
    def post_mirror(account: str | None = None):
        """手动触发一次镜像扫描 (调试/演练用)。"""
        svc = require_service()
        accounts = [account] if account else None
        return {"results": call(svc.mirror_once, accounts)}

    @router.post("/cancel-all")
    def post_cancel_all():
        return call(require_service().cancel_all)

    return router
