"""盘前刷新 (market_context_refresh) 测试 (v0.3.41)。

需求背景: 用户要求「大盘择时 / 情绪周期 / 风险雷达 应该在每天开盘前就取到,
基本上 9:25 竞价结束就该是最新, 这是最迟」。本模块由调度器在工作日 09:20 调用,
任何一路失败都不能挡住开盘。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import market_context_refresh as mcr


def test_premarket_refresh_at_is_before_auction_close():
    """刷新时刻必须早于 09:25 竞价结束, 否则「最迟 9:25」无法兑现。"""
    assert mcr.PREMARKET_REFRESH_AT.hour == 9
    assert mcr.PREMARKET_REFRESH_AT.minute < 25


def test_refresh_all_steps_happy_path(monkeypatch, tmp_path):
    """三路都能跑通时 ok=True, 各步结果都落进返回结构。"""
    from app.services import index_daily_free as idf
    from app.services import market_mainline, regime_builder

    monkeypatch.setattr(
        idf,
        "ensure_timing_index_fresh",
        lambda data_dir=None, **_kw: {"ok": True, "fetched": {"510300": 320}, "reason": "backfilled"},
    )
    monkeypatch.setattr(
        regime_builder,
        "compute_regime_incremental",
        lambda repo, data_dir, **_kw: pl.DataFrame({"date": [date(2026, 11, 5)]}),
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_incremental",
        lambda repo, data_dir, *, kind="concept", **_kw: pl.DataFrame({"n": [1, 2]}),
    )
    import app.api.regime as regime_api
    from app.services import global_risk, premarket

    called: list[str] = []
    monkeypatch.setattr(regime_api, "invalidate_regime_cache", lambda: called.append("regime"))
    monkeypatch.setattr(global_risk, "invalidate_cache", lambda: called.append("global_risk"))
    monkeypatch.setattr(premarket, "invalidate_cache", lambda: called.append("premarket"))

    result = mcr.refresh_market_context(tmp_path)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["timing"]["fetched"] == {"510300": 320}
    assert result["regime"]["rows"] == 1
    assert result["mainline"] == {"concept": 2, "industry": 2}
    assert sorted(result["cache"]["cleared"]) == ["global_risk", "premarket"]
    assert "regime" in called
    assert set(called) == {"regime", "global_risk", "premarket"}


def test_refresh_never_raises_when_every_step_fails(monkeypatch, tmp_path):
    """每一步都抛异常时也只记 errors, 绝不向外抛 (盘前刷新不能挡开盘)。"""
    from app.services import index_daily_free as idf
    from app.services import market_mainline, regime_builder

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(idf, "ensure_timing_index_fresh", _boom)
    monkeypatch.setattr(regime_builder, "compute_regime_incremental", _boom)
    monkeypatch.setattr(market_mainline, "compute_mainline_incremental", _boom)

    result = mcr.refresh_market_context(tmp_path)

    assert result["ok"] is False
    assert len(result["errors"]) >= 3
    assert any("timing" in e for e in result["errors"])
    assert any("regime" in e for e in result["errors"])
    assert any("mainline" in e for e in result["errors"])


def test_refresh_reports_timing_soft_failure_without_raising(monkeypatch, tmp_path):
    """单路 ok=False (如免费源拉空) 记进 errors, 其余步骤照常执行。"""
    from app.services import index_daily_free as idf
    from app.services import market_mainline, regime_builder

    monkeypatch.setattr(
        idf,
        "ensure_timing_index_fresh",
        lambda data_dir=None, **_kw: {"ok": False, "fetched": {}, "reason": "fetch-empty"},
    )
    monkeypatch.setattr(
        regime_builder, "compute_regime_incremental", lambda repo, data_dir, **_kw: pl.DataFrame()
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_incremental",
        lambda repo, data_dir, *, kind="concept", **_kw: pl.DataFrame(),
    )

    result = mcr.refresh_market_context(tmp_path)

    assert result["ok"] is False
    assert any("fetch-empty" in e for e in result["errors"])
    assert result["regime"]["rows"] == 0
    assert result["mainline"] == {"concept": 0, "industry": 0}


def test_refresh_returns_soft_error_when_data_dir_unavailable(monkeypatch):
    """配置不可用时返回 errors 而不是抛。"""

    def _boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr("app.config.settings", None, raising=False)
    # _resolve_data_dir 里 `import settings` 成功但 Path(None) 会抛 → 应被捕获
    result = mcr.refresh_market_context(None)
    assert result["ok"] is False
    assert result["errors"]


def test_refresh_uses_explicit_data_dir(monkeypatch, tmp_path):
    """显式传 data_dir 时不读 settings (桌面端测试/多数据区场景)。"""
    seen: dict = {}

    from app.services import index_daily_free as idf
    from app.services import market_mainline, regime_builder

    def _timing(data_dir=None, **_kw):
        seen["data_dir"] = data_dir
        return {"ok": True, "fetched": {}, "reason": "fresh"}

    monkeypatch.setattr(idf, "ensure_timing_index_fresh", _timing)
    monkeypatch.setattr(
        regime_builder, "compute_regime_incremental", lambda repo, data_dir, **_kw: pl.DataFrame()
    )
    monkeypatch.setattr(
        market_mainline,
        "compute_mainline_incremental",
        lambda repo, data_dir, *, kind="concept", **_kw: pl.DataFrame(),
    )

    mcr.refresh_market_context(tmp_path)

    assert Path(seen["data_dir"]) == tmp_path
