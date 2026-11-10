"""sync_adj_factor 连续全失败短路测试 (0.3.36 事故链修复)。

回归背景: NAS 生产容器 tickflow key 过期时, adj_factor 36 个分块逐个超时
(~40s), 拖长管道持锁时间并间接触发 watchdog 误判。修复后连续 3 个分块
全部失败即提前中止, 剩余标的直接计入 failed_syms (仅用于日志可见化;
返回值语义是 (写入行数, 受影响标的), 全失败时返回 (0, []))。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.rate_limits import ResolvedLimit
from app.tickflow.repository import DataStore, KlineRepository


def _capset_batch(batch: int) -> CapabilitySet:
    """固定 batch 便于用 call 计数断言短路时机; rpm=None → 分块间不 sleep。"""
    return CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=batch, rpm=None)})


def _mock_tickflow_env(monkeypatch, *, resolve_batch: int) -> None:
    """公共 mock: provider=tickflow、不真正发 SDK 请求、不限频。"""
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(
        klines=SimpleNamespace(ex_factors=lambda chunk, **kw: None)))
    monkeypatch.setattr(
        kline_sync, "resolve_limit",
        lambda capset, cap, **kw: ResolvedLimit(batch=resolve_batch, rpm=None),
    )


def test_sync_adj_factor_aborts_after_consecutive_chunk_failures(tmp_path, monkeypatch, caplog):
    """mock SDK 恒抛异常: 10 个分块只应尝试 3 个, 其余 7 个分块被跳过。"""
    repo = KlineRepository(DataStore(tmp_path))
    _mock_tickflow_env(monkeypatch, resolve_batch=1)

    counter = {"n": 0}

    def _deadline(fn, *args, **kwargs):
        counter["n"] += 1
        raise RuntimeError("无除权因子查询权限")

    monkeypatch.setattr(kline_sync, "_sdk_call_with_deadline", _deadline)

    symbols = [f"6{i:05d}.SH" for i in range(10)]  # batch=1 → 10 分块
    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync"):
        written, affected = kline_sync.sync_adj_factor(symbols, repo, _capset_batch(1))

    # 连续 3 个分块失败即短路: 只尝试了 3 次 SDK 调用 (旧实现是 10 次)
    assert counter["n"] == 3, f"应在第 3 个分块失败后短路, 实际尝试 {counter['n']} 次"
    # 全失败 → 无数据可合并, 返回 (0, []); 失败标的故意不进 affected
    # (见 kline_sync.py 部分失败可见化注释: enriched 不重算失败标的)
    assert (written, affected) == (0, [])
    # 短路日志: 剩余 7 个分块被跳过
    assert any("连续 3 个分块全部失败" in r.message and "剩余 7 个分块" in r.message
               for r in caplog.records)
    # 失败可见化: 全部 10 个标的都进了 failed_syms 日志
    assert any("10/10 标的未获取复权因子" in r.message for r in caplog.records)


def test_sync_adj_factor_success_resets_consecutive_counter(tmp_path, monkeypatch):
    """间隔成功不算连续失败: 败-成-败-成… 不触发短路, 全部分块都尝试。"""
    repo = KlineRepository(DataStore(tmp_path))
    _mock_tickflow_env(monkeypatch, resolve_batch=1)

    counter = {"n": 0}

    def _deadline(fn, *args, **kwargs):
        counter["n"] += 1
        # 奇数次失败, 偶数次成功 → 永远凑不满连续 3 次失败
        if counter["n"] % 2 == 1:
            raise RuntimeError("偶发超时")
        return fn()  # 真正调用 ex_factors, 按 chunk 返回该标的的因子数据

    # ex_factors 按分块首个 symbol 返回一条因子事件
    # (trade_date 用 ISO 格式, "20261103" 会被 _normalize_adj_factor 的日期解析丢掉)
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(
        klines=SimpleNamespace(ex_factors=lambda chunk, **kw: {
            chunk[0]: [{"trade_date": "2026-11-03", "ex_factor": 1.0}],
        })))
    monkeypatch.setattr(kline_sync, "_sdk_call_with_deadline", _deadline)

    symbols = [f"6{i:05d}.SH" for i in range(6)]  # 6 分块: 败-成-败-成-败-成
    written, affected = kline_sync.sync_adj_factor(symbols, repo, _capset_batch(1))

    assert counter["n"] == 6, "交替成功不应触发短路"
    assert written == 3
    # 受影响 = 成功拿到因子的 3 个标的 (奇数次调用失败的标的保持旧复权价)
    assert sorted(affected) == sorted(symbols[1::2])


def test_sync_adj_factor_short_circuit_skips_heartbeat(tmp_path, monkeypatch, caplog):
    """短路后不再有 on_chunk_done 心跳 (调用方只是进度展示, 断掉无副作用)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _mock_tickflow_env(monkeypatch, resolve_batch=2)

    heartbeat: list[tuple[int, int]] = []

    def _deadline(fn, *args, **kwargs):
        raise RuntimeError("无除权因子查询权限")

    monkeypatch.setattr(kline_sync, "_sdk_call_with_deadline", _deadline)

    symbols = [f"6{i:05d}.SH" for i in range(10)]  # batch=2 → 5 分块
    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync"):
        written, affected = kline_sync.sync_adj_factor(
            symbols, repo, _capset_batch(2),
            on_chunk_done=lambda c, t: heartbeat.append((c, t)),
        )

    # 3 个分块失败短路 → 心跳只有 3 次, 第 4/5 分块无心跳
    assert heartbeat == [(1, 5), (2, 5), (3, 5)]
    assert (written, affected) == (0, [])
    assert any("连续 3 个分块全部失败" in r.message for r in caplog.records)
