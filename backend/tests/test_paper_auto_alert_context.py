"""模拟盘事件自解释 — 股票名称/规则名/触发原因回填 (task-7 需求1+2)。

用户诉求: ①监控中心「自动跟单触发买入下单」看不出是什么触发的; ②弹窗只有
代码 600327.SH 没有股票名称。

口径:
  - auto_order_events 必须自带 rule_name / match_kind+match_id / reason /
    account_name, 文案一句话讲清「买什么、多少、谁触发、怎么成交」;
  - 股票名称 (name) 在事件生成域拿不到 (无维表访问权), 由 quote_service 的
    _enrich_paper_alert_names 统一回填, 回填后 message 必须重建成带名称版本;
  - 非 paper 来源的事件一律不动。
"""
from __future__ import annotations

from app.services.quote_service import _enrich_paper_alert_names
from app.strategy import paper_auto

SYM = "600327.SH"
NAME = "大东方"


def _auto_event(**over) -> dict:
    ev = {
        "source": "paper",
        "type": "auto_order",
        "severity": "info",
        "ts": 1_760_000_000_000,
        "symbol": SYM,
        "name": "",
        "rule_id": "arule_1",
        "rule_name": "对比·游资·龙头首阴低吸",
        "strategy_id": "zdk_hot_leader_yin",
        "match_kind": "strategy",
        "match_id": "zdk_hot_leader_yin",
        "side": "buy",
        "qty": 4500,
        "order_type": "next_open",
        "account_id": "acc1",
        "account_name": "主账户",
        "reason": "策略进入股票池",
    }
    ev.update(over)
    ev["message"] = paper_auto.format_auto_order_message(ev)
    return ev


# ── auto_order_events 字段契约 ────────────────────────────
def test_auto_order_events_carry_trigger_context():
    orders = [
        {"symbol": SYM, "side": "buy", "qty": 4500, "order_type": "next_open",
         "source": "auto:arule_1",
         "auto_meta": {"rule_name": "对比·游资·龙头首阴低吸", "match_kind": "strategy",
                       "match_id": "zdk_hot_leader_yin", "event_type": "pool_entry"}},
    ]
    (ev,) = paper_auto.auto_order_events(orders, account_id="acc1", account_name="主账户")
    assert ev["rule_id"] == "arule_1"
    assert ev["rule_name"] == "对比·游资·龙头首阴低吸"
    assert ev["match_kind"] == "strategy" and ev["match_id"] == "zdk_hot_leader_yin"
    assert ev["strategy_id"] == "zdk_hot_leader_yin"
    assert ev["reason"] == "策略进入股票池"
    assert ev["account_name"] == "主账户"
    assert ev["order_type"] == "next_open" and ev["qty"] == 4500
    # 生成域没有名称维表 → name 留空, 由调用方回填
    assert ev["name"] == ""
    assert "规则「对比·游资·龙头首阴低吸」" in ev["message"]
    assert "策略进入股票池" in ev["message"] and "次日开盘价成交" in ev["message"]
    assert ev["message"].startswith("账户[主账户] 自动跟单买入")


def test_auto_order_events_unknown_event_type_falls_back():
    """未知触发类型不能把英文 type 直接塞给用户看。"""
    orders = [{"symbol": SYM, "side": "buy", "qty": 100, "order_type": "market",
               "source": "auto:r1", "auto_meta": {"event_type": "weird_type"}}]
    (ev,) = paper_auto.auto_order_events(orders)
    assert ev["reason"] == "监控规则命中"
    assert "weird_type" not in ev["message"]
    # 缺 auto_meta (旧订单/直接调用) 也不能崩
    (ev2,) = paper_auto.auto_order_events(
        [{"symbol": SYM, "side": "sell", "qty": 100, "order_type": "close", "source": "manual"}])
    assert ev2["rule_name"] == "" and ev2["rule_id"] == ""
    assert "当日收盘价成交" in ev2["message"]
    # 未传 account_name → 事件回落 account_id, 文案仍能指明账户 (本需求的核心)
    assert ev2["account_name"] == "default"
    assert ev2["message"].startswith("账户[default] 自动跟单卖出")


def test_format_message_without_name_omits_empty_parens():
    msg = paper_auto.format_auto_order_message({"symbol": SYM, "side": "buy", "qty": 10})
    assert "()" not in msg and SYM in msg


# ── 文案带账户名 ─────────────────────────────────────────
def test_format_message_with_account_name_prefix():
    """account_name 非空 → 第一段带 账户[xxx] 前缀; 为空保持原文案。"""
    ev = _auto_event()
    assert ev["message"].startswith("账户[主账户] 自动跟单买入")
    assert "账户[主账户] 自动跟单买入 600327.SH 4500股" in ev["message"]

    msg_no_acc = paper_auto.format_auto_order_message({
        "symbol": SYM, "side": "buy", "qty": 100,
    })
    assert msg_no_acc.startswith("自动跟单买入")    # 无账户名 → 原样 (向后兼容)
    assert "账户[" not in msg_no_acc


# ── 名称回填 (quote_service 推送前统一处理) ───────────────
def test_enrich_fills_name_and_rebuilds_auto_message():
    ev = _auto_event()
    assert "600327.SH)" not in ev["message"]
    _enrich_paper_alert_names([ev], {SYM: NAME})
    assert ev["name"] == NAME
    # 回填后消息必须带名称, 且规则名/触发原因/成交方式都还在
    assert f"{NAME}({SYM})" in ev["message"]
    assert "规则「对比·游资·龙头首阴低吸」" in ev["message"]
    assert "触发: 策略进入股票池" in ev["message"]


def test_enrich_keeps_existing_name():
    """paper_plans 生成的事件已自带 name, 不能被空映射覆盖或二次加名。"""
    ev = _auto_event(name=NAME)
    before = ev["message"]
    _enrich_paper_alert_names([ev], {})
    assert ev["name"] == NAME and ev["message"] == before


def test_enrich_unknown_symbol_leaves_name_empty():
    """维表没有该标的 (ETF/新上市) → 保持空名称, 文案回落到裸代码, 不报错。"""
    ev = _auto_event(symbol="999999.SH")
    _enrich_paper_alert_names([ev], {SYM: NAME})
    assert ev["name"] == ""
    assert "999999.SH" in ev["message"]


def test_enrich_paper_fill_message_gets_name():
    """成交事件文案由 paper.py 拼 (禁改), 这里靠替换补上名称。"""
    fill = {
        "source": "paper", "type": "fill", "symbol": SYM, "name": "",
        "message": f"模拟盘买入成交 4500股 @ 12.34 ({SYM})",
    }
    _enrich_paper_alert_names([fill], {SYM: NAME})
    assert fill["name"] == NAME
    assert f"{NAME}({SYM})" in fill["message"]
    # 幂等: 二次回填不重复加名
    once = fill["message"]
    _enrich_paper_alert_names([fill], {SYM: NAME})
    assert fill["message"] == once


def test_enrich_ignores_non_paper_sources():
    ev = {"source": "strategy", "type": "pool_entry", "symbol": SYM, "name": "",
          "message": f"策略进入 {SYM}"}
    _enrich_paper_alert_names([ev], {SYM: NAME})
    assert ev["name"] == "" and ev["message"] == f"策略进入 {SYM}"


def test_enrich_handles_empty_input():
    _enrich_paper_alert_names([], {SYM: NAME})       # 不抛
    _enrich_paper_alert_names([_auto_event()], {})   # 空映射: 名称留空, 不崩
