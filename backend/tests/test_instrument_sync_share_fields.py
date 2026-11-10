"""标的维表同步: provider 不提供股本时不覆盖已有值。

回归背景: 部分行情源的 get_instruments 只返回代码/名称/市场/类型, 没有
股本与交易规则字段。旧实现整表覆盖后 float_shares/total_shares 变 null,
enriched 的 turnover_rate 随之全空, 依赖换手率与流通市值的策略 (起爆/5连阳)
直接筛出 0 只 —— 同一个策略换数据源结果就不同。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services import instrument_sync as isync


def _write_existing(data_dir: Path) -> None:
    out = data_dir / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "code": ["600000", "000001"],
        "exchange": ["SH", "SZ"],
        "region": ["CN", "CN"],
        "type": ["stock", "stock"],
        "listing_date": ["1999-11-10", "1991-04-03"],
        "total_shares": [2.9e10, 1.9e10],
        "float_shares": [2.9e10, 1.9e10],
        "tick_size": [0.01, 0.01],
        "limit_up": [11.0, 12.0],
        "limit_down": [9.0, 10.0],
    }).write_parquet(out / "instruments.parquet")


def _provider_rows(*symbols: str) -> list[dict]:
    """模拟只提供标识字段的行情源 (无股本/交易规则)。"""
    return [
        {
            "symbol": s, "name": s, "code": s.split(".")[0],
            "exchange": s.split(".")[1], "region": "CN", "type": "stock",
            "listing_date": None, "total_shares": None, "float_shares": None,
            "tick_size": None, "limit_up": None, "limit_down": None,
        }
        for s in symbols
    ]


def test_share_fields_restored_from_existing_table(tmp_path, monkeypatch) -> None:
    _write_existing(tmp_path)
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH", "000001.SZ"),
    )

    assert isync.sync_instruments(tmp_path) == 2

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(2.9e10)
    assert by_symbol["600000.SH"]["total_shares"] == pytest.approx(2.9e10)
    assert by_symbol["600000.SH"]["tick_size"] == pytest.approx(0.01)
    assert by_symbol["000001.SZ"]["float_shares"] == pytest.approx(1.9e10)


def test_new_symbol_keeps_null_shares_instead_of_faking(tmp_path, monkeypatch) -> None:
    """新上市标的没有历史值 → 保持 null, 不伪造股本。"""
    _write_existing(tmp_path)
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH", "301999.SZ"),
    )

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    new = df.filter(pl.col("symbol") == "301999.SZ")
    assert new.height == 1
    assert new["float_shares"][0] is None
    assert new["total_shares"][0] is None


def test_provider_fields_win_when_present(tmp_path, monkeypatch) -> None:
    """provider 自带股本时以 provider 为准 (不复用旧值)。"""
    _write_existing(tmp_path)
    rows = _provider_rows("600000.SH", "000001.SZ")
    rows[0]["float_shares"] = 1.0e9
    rows[0]["total_shares"] = 2.0e9
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(1.0e9)
    assert by_symbol["600000.SH"]["total_shares"] == pytest.approx(2.0e9)


def test_no_existing_table_is_tolerated(tmp_path, monkeypatch) -> None:
    """首次同步且无历史维表: 不报错, 字段保持 null。"""
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH"),
    )

    assert isync.sync_instruments(tmp_path) == 1

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df["float_shares"][0] is None


def test_partial_provider_fields_filled_per_row(tmp_path, monkeypatch) -> None:
    """provider 只给部分标的股本时, 也要逐行补全 (不能整列跳过)。"""
    _write_existing(tmp_path)
    rows = _provider_rows("600000.SH", "000001.SZ")
    rows[0]["float_shares"] = 5.0e8  # 第一只有值, 第二只仍为空
    rows[0]["total_shares"] = 6.0e8
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(5.0e8)
    assert by_symbol["000001.SZ"]["float_shares"] == pytest.approx(1.9e10)


def test_shrink_guard_keeps_existing_table(tmp_path, monkeypatch) -> None:
    """降级行情源只返回部分市场时必须拒绝写入。

    回归背景 (NAS 事故): tickflow key 过期后免费源接管, 维表被冲成 2823 行
    且只剩沪市 → 深市持仓/选股全部查不到名称与股本, 用户看到「股票名字没了」。
    """
    _write_existing(tmp_path)
    target = tmp_path / "instruments" / "instruments.parquet"
    before = pl.read_parquet(target)
    # 只返回 1 行 (现有 2 行) —— 典型的部分市场源
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600000.SH"),
    )

    assert isync.sync_instruments(tmp_path) == 0  # 拒绝写入

    after = pl.read_parquet(target)
    assert_frame_equal(before, after)  # 旧维表原样保留


def test_missing_symbols_restored_when_new_source_grows(tmp_path, monkeypatch) -> None:
    """新源行数 >= 现有表时写入, 但整行缺失的旧标的要补回, 不能凭空消失。"""
    _write_existing(tmp_path)
    # 3 行 > 现有 2 行, 但两个旧 symbol 都不在 → 补回后应为 5 行
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: _provider_rows("600036.SH", "000002.SZ", "301999.SZ"),
    )

    rows = isync.sync_instruments(tmp_path)
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    symbols = set(df["symbol"].to_list())
    assert symbols == {"600000.SH", "000001.SZ", "600036.SH", "000002.SZ", "301999.SZ"}
    assert rows == len(symbols)
    # 补回的旧标的保留原股本
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["float_shares"] == pytest.approx(2.9e10)
    assert by_symbol["000001.SZ"]["float_shares"] == pytest.approx(1.9e10)


def test_placeholder_name_replaced_from_history(tmp_path, monkeypatch) -> None:
    """免费源把 name 填成代码占位符时, 用历史维表的真名兜回。"""
    _write_existing(tmp_path)
    rows = _provider_rows("600000.SH", "000001.SZ")
    # provider 把 name 写成 symbol/code 占位符
    rows[0]["name"] = "600000.SH"
    rows[1]["name"] = "000001"
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: rows)

    isync.sync_instruments(tmp_path)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["600000.SH"]["name"] == "浦发银行"
    assert by_symbol["000001.SZ"]["name"] == "平安银行"


def test_lookup_names_online_fills_names_and_caches(monkeypatch) -> None:
    """维表缺标的时, 在线名称兜底按代码回填中文名并命中缓存。

    回归背景: NAS 维表被降级源冲成只剩沪市 2823 行, 深市持仓的名称全部查不到,
    持仓表只剩代码。展示层必须有一条不依赖维表的名称来源。
    """
    import app.plugins.tencent.client as tclient
    import app.plugins.tencent.provider as tprov

    isync._name_lookup_cache.clear()
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def fetch(self, symbols):
            calls["n"] += 1
            return {s: ["1", "汉威科技"] for s in symbols}

        def close(self) -> None:
            pass

    monkeypatch.setattr(tclient, "TencentClient", _FakeClient)
    monkeypatch.setattr(
        tprov, "_map_row",
        lambda symbol, fields, ms: {"name": fields[1] if len(fields) > 1 else None},
    )

    got = isync.lookup_names_online(["300007.SZ", "300473.SZ"])
    assert got == {"300007.SZ": "汉威科技", "300473.SZ": "汉威科技"}
    assert calls["n"] == 1

    # 第二次命中进程内缓存, 不再发网络请求
    again = isync.lookup_names_online(["300007.SZ"])
    assert again == {"300007.SZ": "汉威科技"}
    assert calls["n"] == 1
    isync._name_lookup_cache.clear()


def test_lookup_names_online_never_raises_on_failure(monkeypatch) -> None:
    """网络失败只返回空, 绝不把异常抛给调用方 (名称属展示增强)。"""
    import app.plugins.tencent.client as tclient

    isync._name_lookup_cache.clear()

    class _Boom:
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("network down")

    monkeypatch.setattr(tclient, "TencentClient", _Boom)
    assert isync.lookup_names_online(["300007.SZ"]) == {}
    isync._name_lookup_cache.clear()

