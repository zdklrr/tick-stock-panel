"""实盘交易扩展 (官方): 通过 easytrader 驱动同花顺下单客户端。

设计要点:
  - **零侵入**: 不改 paper.py / api/paper.py / 核心 API, 只作为 backend/app/custom 下的
    扩展自动装载; 与模拟盘账务完全隔离 (实盘有自己的配置/委托日志, 绝不写模拟盘台账)。
  - **默认关闭**: enabled=False + dry_run=True 双默认; 必须显式开启才会真正下单。
  - **铁律**: 卖出永不受择时/风险系数影响, 只受「允许卖出」开关与 T+1 可卖数量约束。
  - **单实例**: 同 paper_execution, 用 sqlite EXCLUSIVE 租约保证只有一个进程驱动 GUI。

GUI 自动化固有的短板 (继承 zdk 的教训, 并在界面上明示):
  - 委托为限价单, 价格取当时参考价, 行情跑掉就挂不上, 程序不追价;
  - 只发单, 不查成交、不撤单 (返回的 entrust_no 只代表客户端受理);
  - 单笔 3~5 秒, 客户端必须已登录且窗口未被遮挡;
  - 客户端升级/改名/弹模态框都会导致下单失败。
"""

from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION

EXTENSION_ID = "official.live-trader"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
