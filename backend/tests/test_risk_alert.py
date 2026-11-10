"""风险提示中心单元测试: 只测比较逻辑与降级路径, 不联网。

覆盖:
- 首次运行给基线播报 (否则功能形同虚设)
- 等级跨档 / 系数跳变 / 分数跳变 才播报, 微小抖动不播报
- 去重窗口内不重复
- 雷达 ok=False / 快照为 None 时静默, 且不篡改已提示状态
- 盘前方向翻转才播报
- 大盘状态换档播报, 并在弱势时用 warn
- 一轮最多 1 条, 且保留最严重的那条
- 全局最小间隔内早退
- 状态文件读写幂等、损坏文件按空状态处理
- dispatch 在无 quote_service 时仍落 alert_store
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import risk_alert


def _radar(
    *,
    ok: bool = True,
    level: str = "normal",
    score: float | None = 40.0,
    exposure: float | None = 1.0,
    fetched_at: str = "2026-11-03T09:30:00+08:00",
) -> dict:
    return {
        "ok": ok,
        "level": level,
        "level_text": {"low": "低", "normal": "正常", "elevated": "偏高", "high": "高"}.get(
            level, "未知"
        ),
        "score": score,
        "exposure": exposure,
        "global_exposure": exposure,
        "fetched_at": fetched_at,
        "regions": {
            "us": {"score": 50.0, "exposure": 1.0, "indexes": []},
            "asia": {"score": 60.0, "exposure": 0.9, "indexes": []},
            "hk": {"score": 45.0, "exposure": 1.0, "indexes": []},
        },
        "breadth": {"ok": True, "score": 30.0, "dn_ratio": 0.42, "up": 3000, "down": 2400, "total": 5500},
        "equity": {"ok": True, "factor": 1.0, "drawdown_pct": 3.0, "window_days": 60},
        "advice": "外围偏热, 建议降低仓位",
        "missing": [],
        "indexes": [],
    }


def _premarket(*, ok: bool = True, bias_key: str = "flat", bias: str = "震荡") -> dict:
    return {
        "ok": ok,
        "bias": bias,
        "bias_key": bias_key,
        "strength": "中",
        "strength_key": "mid",
        "probs": {"up": 35.8, "flat": 28.4, "down": 35.8},
        "gap_pct": {"low": -0.86, "high": 0.24},
        "amplitude": 1.1,
        "signal": -0.12,
        "a50": {"price": 12807.1, "chg_pct": -0.43},
        "us": {"chg_pct": -0.61},
        "asia": {"chg_pct": -1.5},
        "cnh": {"price": 6.66, "chg_pct": 0.23, "note": "仅提示"},
        "drivers": [],
        "advice": "外围偏空, 注意开盘缺口",
        "fetched_at": "2026-11-03T09:30:00+08:00",
    }


# ---------------------------------------------------------------- 基线播报


def test_first_run_emits_baseline_radar_event():
    events, state = risk_alert.build_events(
        Path("."), radar=_radar(), premarket=None, regime=None, state={}
    )
    types = [e["type"] for e in events]
    # 首次运行既有雷达基线也有盘前基线, 但一轮最多 1 条
    assert len(events) == 1
    assert types[0] in {"risk_radar", "risk_premarket"}
    assert state["radar_level"] == "normal"


def test_second_run_same_values_emits_nothing():
    # 首轮两个维度都满足播报条件, 但一轮只播 1 条 → 盘前基线被延后, 第二轮补播。
    # 这是刻意的: 若把落选维度的状态也一并推进, 那条提示就永久消失了。
    _, state = risk_alert.build_events(
        Path("."), radar=_radar(), premarket=_premarket(), regime=None, state={}
    )
    events2, state2 = risk_alert.build_events(
        Path("."), radar=_radar(), premarket=_premarket(), regime=None, state=state
    )
    # 第二轮把被挤掉的盘前基线补出来, 之后才彻底静默
    assert [e["type"] for e in events2] == ["risk_premarket"]
    events3, state3 = risk_alert.build_events(
        Path("."), radar=_radar(), premarket=_premarket(), regime=None, state=state2
    )
    assert events3 == []
    assert state3["radar_level"] == "normal"
    assert state3["premarket_bias"] == "flat"


# ---------------------------------------------------------------- 等级/系数变化


def test_level_upgrade_emits_warn_or_critical():
    _, state = risk_alert.build_events(Path("."), radar=_radar(level="normal"), state={})
    events, _ = risk_alert.build_events(
        Path("."), radar=_radar(level="high", exposure=0.55, score=88.0), state=state
    )
    assert len(events) == 1
    assert events[0]["type"] == "risk_radar"
    assert events[0]["severity"] == "critical"
    assert "高" in events[0]["message"]


def test_level_downgrade_is_info():
    _, state = risk_alert.build_events(
        Path("."), radar=_radar(level="elevated", exposure=0.87, score=70.0), state={}
    )
    events, _ = risk_alert.build_events(
        Path("."), radar=_radar(level="normal", exposure=1.0, score=50.0), state=state
    )
    assert len(events) == 1
    assert events[0]["severity"] == "info"


def test_small_score_jitter_does_not_emit():
    _, state = risk_alert.build_events(
        Path("."), radar=_radar(score=40.0, exposure=1.0), state={}
    )
    # 分数只动 2 分、系数不动 → 不打扰
    events, _ = risk_alert.build_events(
        Path("."), radar=_radar(score=42.0, exposure=1.0), state=state
    )
    assert events == []


def test_exposure_step_triggers_even_if_level_unchanged():
    _, state = risk_alert.build_events(
        Path("."), radar=_radar(score=50.0, exposure=1.0), state={}
    )
    # 等级仍是 normal, 但系数掉了 0.15 >= EXPOSURE_STEP
    events, _ = risk_alert.build_events(
        Path("."), radar=_radar(score=50.0, exposure=0.85), state=state
    )
    assert len(events) == 1
    assert "0.85" in events[0]["message"]


# ---------------------------------------------------------------- 去重


def test_dedupe_suppresses_repeat_within_window():
    _, state = risk_alert.build_events(Path("."), radar=_radar(level="normal"), state={})
    events1, state1 = risk_alert.build_events(
        Path("."), radar=_radar(level="high", exposure=0.55, score=90.0), state=state
    )
    assert len(events1) == 1
    # 立刻再来一次同样的跳变 (价格抖动再回跳) → 去重窗口内不重复
    events2, _ = risk_alert.build_events(
        Path("."), radar=_radar(level="normal", exposure=1.0, score=50.0), state=state1
    )
    assert events2 == []


# ---------------------------------------------------------------- 降级路径


def test_radar_ok_false_emits_nothing_and_keeps_state():
    _, state = risk_alert.build_events(Path("."), radar=_radar(level="elevated"), state={})
    before = dict(state)
    events, state2 = risk_alert.build_events(
        Path("."), radar=_radar(ok=False, level="unknown"), state=state
    )
    assert events == []
    # 取数失败不得清空已提示状态, 否则恢复后会把同一档重报一遍
    assert state2["radar_level"] == before["radar_level"]


def test_all_snapshots_none_emits_nothing():
    events, state = risk_alert.build_events(
        Path("."), radar=None, premarket=None, regime=None, state={}
    )
    assert events == []
    assert "radar_level" not in state


def test_broken_state_file_is_treated_as_empty(tmp_path: Path):
    path = tmp_path / "user_data" / "risk_alert_state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert risk_alert.load_state(tmp_path) == {}


def test_state_roundtrip(tmp_path: Path):
    risk_alert.save_state(tmp_path, {"radar_level": "elevated", "radar_score": 72.5})
    got = risk_alert.load_state(tmp_path)
    assert got["radar_level"] == "elevated"
    assert got["radar_score"] == 72.5
    # 文件是合法 UTF-8 JSON
    raw = json.loads((tmp_path / "user_data" / "risk_alert_state.json").read_text(encoding="utf-8"))
    assert raw["radar_level"] == "elevated"


# ---------------------------------------------------------------- 盘前简报


def test_premarket_bias_flip_emits():
    _, state = risk_alert.build_events(
        Path("."), radar=None, premarket=_premarket(bias_key="flat"), regime=None, state={}
    )
    events, _ = risk_alert.build_events(
        Path("."),
        radar=None,
        premarket=_premarket(bias_key="bear", bias="偏空"),
        regime=None,
        state=state,
    )
    assert len(events) == 1
    assert events[0]["type"] == "risk_premarket"
    assert events[0]["severity"] == "warn"


def test_premarket_same_bias_no_emit():
    _, state = risk_alert.build_events(
        Path("."), premarket=_premarket(bias_key="flat"), state={}
    )
    events, _ = risk_alert.build_events(
        Path("."), premarket=_premarket(bias_key="flat"), state=state
    )
    assert events == []


# ---------------------------------------------------------------- 大盘状态


def test_regime_state_change_emits_and_weak_is_warn():
    _, state = risk_alert.build_events(Path("."), radar=None, regime={"state": "range", "score": 50}, state={})
    events, _ = risk_alert.build_events(
        Path("."), radar=None, regime={"state": "weak", "score": 20}, state=state
    )
    assert len(events) == 1
    assert events[0]["type"] == "risk_regime"
    assert events[0]["severity"] == "warn"
    assert "弱势" in events[0]["message"]


# ---------------------------------------------------------------- 全局频控


def test_at_most_one_event_per_tick_keeps_severity_order():
    state = {
        "radar_level": "normal",
        "radar_score": 50.0,
        "radar_exposure": 1.0,
        "premarket_bias": "flat",
        "regime_state": "range",
        "last_radar_ms": 0,
        "last_premarket_ms": 0,
        "last_regime_ms": 0,
    }
    events, _ = risk_alert.build_events(
        Path("."),
        radar=_radar(level="high", score=90.0, exposure=0.5),
        premarket=_premarket(bias_key="bear", bias="偏空"),
        regime={"state": "weak", "score": 15},
        state=state,
    )
    assert len(events) == 1
    assert events[0]["severity"] == "critical"


def test_min_tick_interval_short_circuits(tmp_path: Path):
    risk_alert.save_state(tmp_path, {"last_tick_ms": risk_alert._now_ms()})
    # 已刚刚 tick 过 → 直接早退, 不会去取数
    assert risk_alert.evaluate(tmp_path, fetch=False) == []


# ---------------------------------------------------------------- dispatch


def test_dispatch_writes_alert_store_without_quote_service(tmp_path: Path):
    events = [
        {
            "ts": risk_alert._now_ms(),
            "source": "risk",
            "type": "risk_radar",
            "symbol": "",
            "name": "",
            "message": "风险雷达: 外围风险 偏高",
            "price": None,
            "change_pct": 0,
            "severity": "warn",
            "rule_id": None,
            "rule_name": "",
            "strategy_id": None,
            "signals": [],
            "conditions": [],
            "logic": "and",
        }
    ]
    risk_alert.dispatch(tmp_path, events, None)
    from app.services import alert_store

    rows = alert_store.list_recent(tmp_path, days=7, limit=10)
    assert any(r.get("source") == "risk" for r in rows)


def test_dispatch_empty_is_noop(tmp_path: Path):
    risk_alert.dispatch(tmp_path, [], None)
    assert not (tmp_path / "user_data" / "alerts.jsonl").exists()


# ---------------------------------------------------------------- 消息内容


def test_radar_message_contains_key_numbers():
    events, _ = risk_alert.build_events(
        Path("."),
        radar=_radar(level="elevated", score=72.0, exposure=0.87),
        state={},
    )
    msg = events[0]["message"]
    assert "偏高" in msg
    assert "72" in msg
    assert "0.87" in msg
    assert "跌家占比" in msg
    assert "外围偏热" in msg


@pytest.mark.parametrize(
    ("level", "expected"),
    [("low", "info"), ("normal", "info"), ("elevated", "warn"), ("high", "critical")],
)
def test_severity_mapping(level: str, expected: str):
    assert risk_alert._severity_for_level(level) == expected
