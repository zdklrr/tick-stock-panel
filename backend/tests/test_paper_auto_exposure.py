"""模拟盘「策略+风险自动把控仓位」测试 — 动态仓位系数 / 人为闸门默认关闭。

口径:
  - 仓位 = qty_planned × adj, 其中 adj = plan_adjust × radar_adjust
    (plan_adjust = 当前大盘倍率 / 建计划时冻结的倍率, radar_adjust = 外围 × 回撤);
  - 系数只影响「买多少」, **绝不影响卖出**: 止损/目标1/目标2 的触发与数量与系数无关;
  - 人为闸门 (max_symbols / max_total_exposure_pct) 默认关闭, 打开后才生效;
  - 风险雷达不可用 (模块缺失 / 取数失败) 必须降级为 1.0, 不能抛异常、不能停摆。
纯函数口径见 test_trade_plan.py, 计划流转口径见 test_paper_plans.py。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_plans, trade_plan

SYM = "600519.SH"
SYM2 = "000002.SZ"
DAY = date(2026, 11, 2)
DAY2 = DAY + timedelta(days=1)


# ── 夹具 ────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _neutral_radar(monkeypatch):
    """默认把风险雷达钉成中性 (1.0 × 1.0); 需要真实系数的用例自己覆盖。"""
    try:
        from app.services import global_risk
    except ImportError:  # 雷达模块未部署 → _radar_coeffs 自己就降级为 1.0
        pass
    else:
        monkeypatch.setattr(
            global_risk, "exposure_for_paper", lambda *a, **kw: (1.0, 1.0), raising=False
        )
    paper_plans.invalidate_radar_cache()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """默认账户 (100 万) + 时间冻结到 DAY 盘中。"""
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(
        paper_plans, "cn_now", lambda: datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    )
    paper.create_account(tmp_path, 1_000_000)
    yield tmp_path
    paper_plans.invalidate_radar_cache()


def _advance_day(monkeypatch, day: date) -> None:
    """把「今天」推到 day (跨日后 T+1 可卖放行)。"""
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    monkeypatch.setattr(paper_plans, "cn_today", lambda: day)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(day, time(10, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(
        paper_plans, "cn_now", lambda: datetime.combine(day, time(10, 0), tzinfo=CN_TZ)
    )


def _set_regime(tmp_path, state: str, *, day: date = DAY, score: float = 50.0) -> None:
    """写一行大盘情绪 (覆盖式), 并让读缓存失效 —— 同一测试内要能连续改判。"""
    d = tmp_path / "regime_history"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "date": [day],
            "state": [state],
            "score": [float(score)],
        },
        schema={"date": pl.Date, "state": pl.String, "score": pl.Float64},
    ).write_parquet(d / "part.parquet")
    from app.services import regime_builder

    # 读缓存按 (路径, mtime_ns, 大小) 判定; 同一秒内改写同一份文件必须手工失效
    regime_builder._REGIME_HISTORY_CACHE = None
    regime_builder._REGIME_HISTORY_SIG = None


def _new_plan(tmp_path, **overrides) -> dict:
    payload = {
        "symbol": SYM,
        "name": "贵州茅台",
        "entry_low": 98.0,
        "entry_high": 102.0,
        "stop": 96.0,
        "qty_planned": 1000,
    }
    payload.update(overrides)
    return paper_plans.create_plan(tmp_path, payload)


def _buy_orders(env) -> list[dict]:
    return [o for o in paper.load_orders(env) if o["side"] == "buy"]


# ── 纯函数: position_adjust ─────────────────────────────
def test_position_adjust_neutral_is_one():
    """无择时、无雷达 → 系数 1.0 (不加不减), 原因里写明无择时。"""
    info = trade_plan.position_adjust(None)
    assert info["adj"] == pytest.approx(1.0)
    assert info["plan_adjust"] == pytest.approx(1.0)
    assert info["radar_adjust"] == pytest.approx(1.0)
    assert "无择时" in info["reason"]


def test_position_adjust_regime_improves_scales_up():
    """建计划时震荡(0.75), 下单时仍震荡 → 只算变化量, 系数 1.0 (不重复计算)。"""
    state = {"state": "range", "label": "震荡"}
    same = trade_plan.position_adjust(state, timing_scale=0.75)
    assert same["adj"] == pytest.approx(1.0)

    # 建计划时弱势(0.3), 下单时转震荡(0.75) → 1.0/0.75×0.75/0.3 ... adj = 0.75/0.3 = 2.5
    better = trade_plan.position_adjust(state, timing_scale=0.3)
    assert better["adj"] == pytest.approx(2.5)
    assert "震荡" in better["reason"] and "×2.50" in better["reason"]


def test_position_adjust_regime_worsens_scales_down():
    """建计划时强势(1.2), 下单时转弱势(0.3) → adj = 0.3/1.2 = 0.25 (缩量而非放弃)。"""
    info = trade_plan.position_adjust({"state": "weak", "label": "弱势"}, timing_scale=1.2)
    assert info["adj"] == pytest.approx(0.25)
    assert info["adj"] < 0.5


def test_position_adjust_clamps_to_legal_range():
    """极端输入夹在 [0.2, 3.0]: 再好的大盘也不会把仓位放大到离谱。"""
    hot = trade_plan.position_adjust({"state": "strong", "label": "强势"}, timing_scale=0.3)
    assert hot["adj"] == pytest.approx(trade_plan.ADJ_CAP)

    cold = trade_plan.position_adjust(
        {"state": "weak", "label": "弱势"}, timing_scale=1.2, global_exposure=0.5
    )
    assert cold["adj"] == pytest.approx(trade_plan.ADJ_FLOOR)


def test_position_adjust_clamps_radar_separately():
    """雷达系数单独夹在 [0.5, 1.5] (脏数据不会穿透到总系数)。"""
    info = trade_plan.position_adjust(
        None, global_exposure=3.0, equity_factor=3.0
    )
    assert info["radar_adjust"] == pytest.approx(trade_plan.RADAR_CAP)
    lo = trade_plan.position_adjust(None, global_exposure=0.1, equity_factor=0.1)
    assert lo["radar_adjust"] == pytest.approx(trade_plan.RADAR_FLOOR)


def test_position_adjust_multiplies_global_and_equity_not_composite():
    """radar_adjust = 外围系数 × 回撤系数 (不是雷达合成的 exposure)。"""
    info = trade_plan.position_adjust(None, global_exposure=0.8, equity_factor=0.9)
    assert info["radar_adjust"] == pytest.approx(0.72)
    assert info["adj"] == pytest.approx(0.72)
    assert "外围×0.80" in info["reason"] and "回撤×0.90" in info["reason"]


# ── 建计划冻结的择时快照 ────────────────────────────────
def test_plan_freezes_timing_snapshot(env):
    """建计划时把当时的大盘倍率冻结进 timing_scale, 并记下情绪标签。"""
    _set_regime(env, "weak", score=12.0)
    plan = _new_plan(env)
    merged = trade_plan.merge_cfg(None)
    assert plan["timing_scale"] == pytest.approx(0.3)
    assert plan["exposure"] == pytest.approx(0.3)
    assert plan["state"] == "weak"
    assert plan["regime_label"] == "弱势"
    assert plan["last_adj"] is None and plan["adj_history"] == []
    assert trade_plan.exposure_multiplier({"state": "weak"}, merged) == pytest.approx(0.3)


# ── 买入: 按系数折算股数 ────────────────────────────────
def test_try_buy_scales_down_when_regime_worsens(env):
    """建计划(震荡 0.75)后大盘转弱(0.3) → 1000 股缩到 400 股, 并留痕。"""
    _set_regime(env, "range", score=50.0)
    plan = _new_plan(env)
    _set_regime(env, "weak", score=15.0)

    events = paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert len(events) == 1 and events[0]["type"] == "plan_buy"
    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == 400
    assert "仓位系数 0.40" in events[0]["message"]

    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "bought"
    assert got["last_adj"] == pytest.approx(0.4)
    assert "弱势×0.40" in got["last_adj_reason"]
    assert len(got["adj_history"]) == 1
    assert got["adj_history"][0]["adj"] == pytest.approx(0.4)


def test_try_buy_scales_up_when_regime_turns_strong(env):
    """建计划(弱势 0.3)后大盘转强(1.2) → 1000 股放大到 3000 股 (夹在 3.0 上限)。"""
    _set_regime(env, "weak", score=10.0)
    plan = _new_plan(env)
    _set_regime(env, "strong", score=88.0)

    events = paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert len(events) == 1
    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == 3000
    got = paper_plans.get_plan(env, plan["id"])
    assert got["last_adj"] == pytest.approx(trade_plan.ADJ_CAP)


def test_try_buy_keeps_min_lot_when_scaled_below_one_lot(env):
    """折算后不足一手也要买最小一手 (弱势保留参与度, 转强才有底仓可加)。"""
    _set_regime(env, "strong", score=90.0)
    plan = _new_plan(env, qty_planned=100)
    _set_regime(env, "weak", score=10.0)

    paper_plans.evaluate_plans(env, {SYM: 100.0})
    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == paper.LOT_SIZE
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"


def test_try_buy_limited_by_available_cash(env):
    """系数要买 1000 股但现金只够 200 股 → 按现金反推最大整手, 不整单放弃。"""
    acc = paper.get_account(env)
    acc["cash"] = 25_000.0
    paper.save_account(env, acc)
    plan = _new_plan(env)

    paper_plans.evaluate_plans(env, {SYM: 100.0})
    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == 200
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"


def test_try_buy_leaves_error_when_cash_below_one_lot(env):
    """现金连一手都不够 → 不挂单、留 last_error、计划仍 pending 等下轮。"""
    acc = paper.get_account(env)
    acc["cash"] = 500.0
    paper.save_account(env, acc)
    plan = _new_plan(env)

    assert paper_plans.evaluate_plans(env, {SYM: 100.0}) == []
    assert _buy_orders(env) == []
    got = paper_plans.get_plan(env, plan["id"])
    assert got["status"] == "pending"
    assert "资金" in (got["last_error"] or "")


# ── 卖出: 系数绝不参与 (铁律) ───────────────────────────
def test_sell_qty_ignores_position_adjust(env, monkeypatch):
    """放大系数买入 1600 股后, 止损仍是「清仓」—— 卖出数量与系数无关。"""
    _set_regime(env, "range", score=50.0)
    plan = _new_plan(env)
    _set_regime(env, "strong", score=90.0)          # adj = 1.2/0.75 = 1.6
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert _buy_orders(env)[0]["qty"] == 1600
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "holding"

    _advance_day(monkeypatch, DAY2)                  # 跨日 → T+1 可卖
    # 大盘同时转弱 (系数会缩小), 但止损逻辑不能因此少卖一股
    _set_regime(env, "weak", score=5.0)
    events = paper_plans.evaluate_plans(env, {SYM: 95.0})
    assert len(events) == 1 and "触及止损" in events[0]["message"]
    sell = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sell) == 1 and sell[0]["qty"] == 1600


@pytest.mark.parametrize(
    ("gexp", "held_expect"),
    [(0.5, 500), (1.5, 1500)],   # radar_adjust 取下限 0.5 与上限 1.5 两个极端
)
def test_sell_trigger_and_qty_identical_at_extreme_adj(env, monkeypatch, gexp, held_expect):
    """相邻的 0.2 / 3.0 级极端系数下, 止损失败触发与卖出股数完全由持仓决定。

    这是 task-6 的铁律: 已经持仓的保护绝不打折 —— 系数只影响「买了多少」,
    买定之后止损清仓的判定与数量与系数再无关系。
    """
    monkeypatch.setattr(
        paper_plans, "_radar_coeffs", lambda *a, **kw: {
            "ok": True, "global_exposure": gexp, "equity_factor": 1.0
        }
    )
    _set_regime(env, "range", score=50.0)
    plan = _new_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert _buy_orders(env)[0]["qty"] == held_expect

    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    assert paper_plans.get_plan(env, plan["id"])["status"] == "holding"

    # 触止损时把系数打到另一个极端: 卖出数量必须仍等于全部持仓
    other = 1.5 if gexp == 0.5 else 0.5
    monkeypatch.setattr(
        paper_plans, "_radar_coeffs", lambda *a, **kw: {
            "ok": True, "global_exposure": other, "equity_factor": 1.0
        }
    )
    _advance_day(monkeypatch, DAY2)
    events = paper_plans.evaluate_plans(env, {SYM: 95.0})
    assert len(events) == 1 and "触及止损" in events[0]["message"]
    sell = [o for o in paper.load_orders(env) if o["side"] == "sell"]
    assert len(sell) == 1
    assert sell[0]["qty"] == held_expect


# ── 人为闸门: 默认关闭, 打开才生效 ──────────────────────
def test_max_symbols_default_zero_means_unlimited(env):
    """默认 max_symbols=0 → 不设只数上限, 第 2、3 只照常买入。"""
    assert paper_plans.load_cfg(env)["max_symbols"] == 0
    for sym in (SYM, SYM2, "600036.SH"):
        _new_plan(env, symbol=sym)
        paper_plans.evaluate_plans(env, {sym: 100.0})
        paper.evaluate_intraday(env, {sym: 100.0})
        paper_plans.evaluate_plans(env, {sym: 100.0})
    held = {s for s, p in paper.load_positions(env).items() if int(p.get("qty") or 0) > 0}
    assert held == {SYM, SYM2, "600036.SH"}


def test_max_symbols_blocks_new_symbol_only(env):
    """cfg.max_symbols=1: 已持 1 只时新标的被拦 (留 last_error), 加仓不受限。"""
    paper_plans.save_cfg(env, {"max_symbols": 1})
    _new_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})

    second = _new_plan(env, symbol=SYM2)
    assert paper_plans.evaluate_plans(env, {SYM2: 100.0}) == []
    got = paper_plans.get_plan(env, second["id"])
    assert got["status"] == "pending"
    assert "上限" in (got["last_error"] or "") and "max_symbols" in (got["last_error"] or "")


def test_max_total_exposure_pct_compresses_buy(env):
    """cfg.max_total_exposure_pct=5 → 100 万权益只允许再买 5 万 → 500 股。"""
    paper_plans.save_cfg(env, {"max_total_exposure_pct": 5.0})
    plan = _new_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == 500
    assert paper_plans.get_plan(env, plan["id"])["status"] == "bought"


def test_max_total_exposure_pct_blocks_when_already_full(env):
    """持仓已占总仓位上限 → 不再买入, 留 last_error 等下轮。"""
    paper_plans.save_cfg(env, {"max_total_exposure_pct": 5.0})
    _new_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})
    paper.evaluate_intraday(env, {SYM: 100.0})
    paper_plans.evaluate_plans(env, {SYM: 100.0})     # 已持 500 股 = 5 万市值

    second = _new_plan(env, symbol=SYM2)
    assert paper_plans.evaluate_plans(env, {SYM2: 100.0}) == []
    got = paper_plans.get_plan(env, second["id"])
    assert got["status"] == "pending"
    assert "总仓位上限" in (got["last_error"] or "")


# ── 风险雷达: 取数 / 降级 / 缓存 ─────────────────────────
def _global_risk_module():
    """取真雷达模块; 未部署则跳过 (契约要求可降级, 不存在也算合法)。"""
    return pytest.importorskip("app.services.global_risk")


def test_radar_coeffs_reads_global_and_equity(env, monkeypatch):
    """雷达可用时取 (外围系数, 回撤系数), 并标记 ok。"""
    gr = _global_risk_module()
    monkeypatch.setattr(gr, "exposure_for_paper", lambda d, a="default": (0.78, 0.92))
    paper_plans.invalidate_radar_cache()

    got = paper_plans._radar_coeffs(env, "default")
    assert got["ok"] is True
    assert got["global_exposure"] == pytest.approx(0.78)
    assert got["equity_factor"] == pytest.approx(0.92)


def test_radar_coeffs_degrades_when_module_missing(env, monkeypatch):
    """雷达模块没部署 (ImportError) → 系数 1.0, 不抛异常。"""
    import app.services as services_pkg

    class _Blocker:
        """让 app.services.global_risk 在导入阶段就抛 ImportError (模拟模块被裁剪)。"""

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "app.services.global_risk":
                raise ImportError(f"{fullname} 未部署")
            return None

    monkeypatch.delitem(sys.modules, "app.services.global_risk", raising=False)
    monkeypatch.delattr(services_pkg, "global_risk", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])
    paper_plans.invalidate_radar_cache()

    got = paper_plans._radar_coeffs(env, "default")
    assert got == {**paper_plans._NEUTRAL_RADAR}
    assert got["ok"] is False
    assert got["global_exposure"] == pytest.approx(1.0)


def test_radar_coeffs_degrades_when_fetch_raises(env, monkeypatch):
    """雷达取数抛异常 → 系数 1.0 且不向上传播 (不能拖垮行情轮询)。"""
    gr = _global_risk_module()

    def boom(*_a, **_kw):
        raise RuntimeError("外围行情源挂了")

    monkeypatch.setattr(gr, "exposure_for_paper", boom)
    paper_plans.invalidate_radar_cache()

    got = paper_plans._radar_coeffs(env, "default")
    assert got["global_exposure"] == pytest.approx(1.0)
    assert got["equity_factor"] == pytest.approx(1.0)
    assert got["ok"] is False


def test_radar_coeffs_cached_within_ttl(env, monkeypatch):
    """TTL 内复用同一份系数: 行情轮询每轮都调, 不能每轮联网取数。"""
    calls: list[str] = []

    def fake(data_dir, account_id="default"):
        calls.append(account_id)
        return (0.8, 0.9)

    gr = _global_risk_module()
    monkeypatch.setattr(gr, "exposure_for_paper", fake)
    paper_plans.invalidate_radar_cache()

    first = paper_plans._radar_coeffs(env, "default")
    second = paper_plans._radar_coeffs(env, "default")
    assert len(calls) == 1
    assert first == second
    # 显式失效后才会重新取数
    paper_plans.invalidate_radar_cache()
    paper_plans._radar_coeffs(env, "default")
    assert len(calls) == 2


def test_radar_adjust_enters_buy_quantity(env, monkeypatch):
    """雷达系数确实进到买入股数: 外围 0.8 × 回撤 0.9 = 0.72 → 1000 股变 700 股。"""
    monkeypatch.setattr(
        paper_plans, "_radar_coeffs", lambda *a, **kw: {
            "ok": True, "global_exposure": 0.8, "equity_factor": 0.9
        }
    )
    plan = _new_plan(env)
    paper_plans.evaluate_plans(env, {SYM: 100.0})

    orders = _buy_orders(env)
    assert len(orders) == 1 and orders[0]["qty"] == 700
    got = paper_plans.get_plan(env, plan["id"])
    assert got["last_adj"] == pytest.approx(0.72)
    assert "外围×0.80" in got["last_adj_reason"]


# ── 配置项 / 文案 ───────────────────────────────────────
def test_cfg_roundtrip_and_labels_include_new_keys(env):
    """两个新参数能存能读, 越界拒绝, 并出现在面板文案里。"""
    saved = paper_plans.save_cfg(env, {"max_symbols": 8, "max_total_exposure_pct": 60.0})
    assert saved["max_symbols"] == 8 and saved["max_total_exposure_pct"] == 60.0

    loaded = paper_plans.load_cfg(env)
    assert loaded["max_symbols"] == 8
    assert loaded["max_total_exposure_pct"] == pytest.approx(60.0)

    labels = paper_plans.labels()
    assert labels["max_symbols"] == "最多持仓只数(0=不限)"
    assert labels["max_total_exposure_pct"] == "总仓位上限%"

    # 越界值回落默认 (脏配置不能炸掉计划), 0 只 = 不限必须合法
    assert trade_plan.merge_cfg({"max_symbols": 999})["max_symbols"] == 0
    assert trade_plan.merge_cfg({"max_total_exposure_pct": 200.0})["max_total_exposure_pct"] == 100.0
    assert trade_plan.merge_cfg({"max_symbols": 0})["max_symbols"] == 0
    assert trade_plan.merge_cfg({"max_symbols": "abc"})["max_symbols"] == 0
    # 非法值不写进配置 (save_cfg 走同一个 merge)
    assert paper_plans.save_cfg(env, {"max_symbols": -5})["max_symbols"] == 0


def test_adj_history_is_capped(env):
    """系数留痕最多保留最近 20 条 (长期挂着也不让计划文件无限膨胀)。"""
    plan = _new_plan(env)
    for i in range(25):
        paper_plans._note_adj(plan, {"adj": 1.0 + i * 0.01, "reason": f"第{i}次"})
    assert len(plan["adj_history"]) == 20
    assert plan["adj_history"][-1]["reason"] == "第24次"
    assert plan["last_adj"] == pytest.approx(1.24)


# ── HTTP 契约: 新参数能被前端读写 ───────────────────────
@pytest.fixture
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.paper import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=env),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def test_api_plan_config_exposes_new_keys(env, client):
    """面板可从 /plan_config 读到两个新参数的默认值、文案, 并能写回。"""
    payload = client.get("/api/paper/plan_config?account_id=default").json()
    assert payload["cfg"]["max_symbols"] == 0            # 默认不限
    assert payload["cfg"]["max_total_exposure_pct"] == pytest.approx(100.0)
    assert payload["labels"]["max_symbols"] == "最多持仓只数(0=不限)"
    assert payload["labels"]["max_total_exposure_pct"] == "总仓位上限%"

    r = client.put(
        "/api/paper/plan_config?account_id=default",
        json={"cfg": {"max_symbols": 6, "max_total_exposure_pct": 70.0}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cfg"]["max_symbols"] == 6
    assert r.json()["cfg"]["max_total_exposure_pct"] == pytest.approx(70.0)
    # 落盘后再读仍是新值
    again = client.get("/api/paper/plan_config?account_id=default").json()
    assert again["cfg"]["max_symbols"] == 6


def test_api_plan_carries_timing_snapshot(env, client):
    """计划响应体带出冻结的择时快照与系数留痕字段 (前端可直接展示)。"""
    _set_regime(env, "lean_strong", score=67.0)
    body = {
        "symbol": SYM, "name": "贵州茅台", "entry_low": 98.0, "entry_high": 102.0,
        "stop": 96.0, "qty_planned": 1000,
    }
    r = client.post("/api/paper/plans?account_id=default", json=body)
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["timing_scale"] == pytest.approx(1.0)
    assert plan["state"] == "lean_strong"
    assert plan["regime_label"] == "偏强"
    assert plan["last_adj"] is None
    assert plan["adj_history"] == []
