"""看门狗触发逻辑测试 (不真退出进程)。"""
from __future__ import annotations

import asyncio

from app.watchdog import HealthWatchdog, default_probe


async def _run_watchdog(probe_results, *, threshold=2, interval=0.01, timeout=0.2):
    exits: list[int] = []
    idx = 0

    def probe() -> None:
        nonlocal idx
        if idx < len(probe_results):
            result = probe_results[idx]
            idx += 1
            if isinstance(result, BaseException):
                raise result
        # 脚本耗尽后恒为成功

    wd = HealthWatchdog(
        probe,
        exit_cb=exits.append,
        interval_s=interval,
        probe_timeout_s=timeout,
        failure_threshold=threshold,
    )
    wd.start()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if exits or wd._task.done():
            break
    await wd.stop()
    return exits


async def test_consecutive_failures_trigger_exit() -> None:
    exits = await _run_watchdog([RuntimeError("wedge"), TimeoutError("wedge")])
    assert exits == [70]


async def test_success_resets_failure_counter() -> None:
    # 失败 1 次 → 成功 → 再失败 1 次: 未达连续阈值, 不退出。
    exits = await _run_watchdog([RuntimeError("slow"), None, RuntimeError("slow")])
    assert exits == []


async def test_stop_cancels_task_parked_inside_probe() -> None:
    # 回归: stop() 的 cancel 若落在 wait_for(to_thread) 内, 旧实现把
    # CancelledError 当"探测失败"吞掉 → 循环不退出 → stop() 永久挂起。
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()

    def blocking_probe() -> None:
        entered.set()
        release.wait(5)

    wd = HealthWatchdog(
        blocking_probe,
        exit_cb=lambda code: None,
        interval_s=0.01,
        probe_timeout_s=10,
        failure_threshold=99,
    )
    wd.start()
    # 等探测线程真正进入阻塞 (此刻任务恰好停在 wait_for 内), 再 stop
    await asyncio.to_thread(entered.wait, 2)
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(wd.stop(), timeout=3)
        assert time.monotonic() - t0 < 3, "stop() 应立即取消探测中的任务, 而非挂起"
    finally:
        release.set()


async def test_default_probe_passes_on_healthy_resources() -> None:
    import threading

    lock = threading.Lock()
    default_probe(lock)  # 不抛即通过
    default_probe(None)


async def test_default_probe_write_lock_busy_is_not_failure() -> None:
    # 回归 (2026-11 NAS 事故): 每日管道 compute_enriched / enriched warmup
    # 长时间持有 repo._write_lock 属正常后台忙, 写锁 acquire 超时不应抛异常,
    # 否则 watchdog 连续失败 → 进程自杀重启 → regime/mainline 阶段没跑到。
    import threading

    lock = threading.Lock()
    with lock:  # 先占用写锁, 模拟后台管道持锁
        # 不抛即通过 (旧实现会 raise TimeoutError("repository write lock unavailable"))
        default_probe(lock)
    # 释放后同样通过
    default_probe(lock)


async def test_default_probe_collect_failure_still_raises() -> None:
    # collect 挂死仍是僵死信号 (2026-09-07 事故形态), 必须照常计失败。
    import threading

    from app import watchdog as wd_mod

    lock = threading.Lock()

    def _boom(lf):
        raise RuntimeError("collect wedged")

    original = wd_mod.guarded_collect
    try:
        wd_mod.guarded_collect = _boom
        import pytest

        with pytest.raises(RuntimeError, match="collect wedged"):
            wd_mod.default_probe(lock)
    finally:
        wd_mod.guarded_collect = original
