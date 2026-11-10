"""腾讯行情插件契约测试。

不依赖真实网络与 Key:
  - 客户端层用假 HTTP 响应验证 GBK 解码、缺失行跳过、异常转 TencentError;
  - provider 层用假客户端注入, 验证字段映射与单位换算、停牌零值、五档解析、
    软失败语义与能力声明。
"""

from __future__ import annotations

from datetime import datetime

import httpx
import polars as pl
import pytest

from app.market_time import CN_TZ
from app.plugins.tencent.client import (
    TencentClient,
    TencentError,
    parse_quotes,
    to_standard_symbol,
    to_tencent_code,
)
from app.plugins.tencent.provider import (
    TencentProvider,
    _depth_entry,
    _map_row,
    _universe_symbols,
    availability,
)

_QUOTE_TIME = "20261102150003"
_EXPECTED_TS = int(
    datetime(2026, 11, 2, 15, 0, 3, tzinfo=CN_TZ).timestamp() * 1000
)


# ===== 构造实测行 =====


def _fields(over: dict[int, str] | None = None) -> list[str]:
    """按 600000.SH 实测快照构造字段数组(50 项, 覆盖到索引 45)。"""
    values = [""] * 50
    values[0] = "1"
    values[1] = "浦发银行"
    values[2] = "600000"
    values[3] = "10.06"  # 最新价
    values[4] = "10.02"  # 昨收
    values[5] = "9.98"  # 今开
    values[6] = "1164223"  # 成交量(手)
    for level in range(5):
        values[9 + level * 2] = f"{10.05 - level * 0.01:.2f}"
        values[10 + level * 2] = str(1000 * (level + 1))
        values[19 + level * 2] = f"{10.07 + level * 0.01:.2f}"
        values[20 + level * 2] = str(900 * (level + 1))
    values[30] = _QUOTE_TIME
    values[31] = "0.04"  # 涨跌额
    values[32] = "0.40"  # 涨跌幅(百分数)
    values[33] = "10.10"
    values[34] = "9.95"
    values[36] = "1164223"
    values[37] = "11697.44"  # 成交额(万元)
    values[38] = "1.23"  # 换手率(百分数)
    values[43] = "1.50"  # 振幅(百分数)
    values[44] = "2950.00"
    values[45] = "2950.00"
    for index, value in (over or {}).items():
        values[index] = value
    return values


def _line(code: str, fields: list[str]) -> str:
    return "v_" + code + '="' + "~".join(fields) + '";'


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.content = text.encode("gbk")
        self.status_code = status_code


class _FakeHTTP:
    """假 httpx.Client: 按顺序返回响应或抛异常。"""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []
        self.closed = False

    def get(self, url: str):
        self.urls.append(url)
        item = self._responses.pop(0) if self._responses else _FakeResponse("")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class _FakeTencentClient:
    """假 provider 客户端: 只回请求里存在的 symbol。"""

    def __init__(self, rows: dict[str, list[str]] | None = None, error=None) -> None:
        self.rows = rows or {}
        self.error = error
        self.calls: list[list[str]] = []
        self.closed = False

    def fetch(self, symbols: list[str]) -> dict[str, list[str]]:
        self.calls.append(list(symbols))
        if self.error is not None:
            raise self.error
        wanted = set(symbols)
        return {s: f for s, f in self.rows.items() if s in wanted}

    def close(self) -> None:
        self.closed = True


def _provider(rows=None, error=None) -> TencentProvider:
    provider = TencentProvider()
    provider._client = _FakeTencentClient(rows, error)
    return provider


# ===== 代码映射 =====


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", "sh600000"),
        ("000001.SZ", "sz000001"),
        ("430047.BJ", "bj430047"),
        ("510300.SH", "sh510300"),
        ("sh600000", "sh600000"),
        ("600000.HK", None),
        ("600000", None),
        ("", None),
        (None, None),
    ],
)
def test_to_tencent_code(symbol, expected) -> None:
    assert to_tencent_code(symbol) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("sh600000", "600000.SH"),
        ("sz399001", "399001.SZ"),
        ("bj430047", "430047.BJ"),
        ("SH600000", "600000.SH"),
        ("hk600000", None),
        ("sh60000", None),
    ],
)
def test_to_standard_symbol(code, expected) -> None:
    assert to_standard_symbol(code) == expected


# ===== 客户端解析 =====


def test_parse_quotes_mixed_instruments() -> None:
    text = "\n".join(
        [
            _line("sh600000", _fields()),
            _line("sh000001", _fields({1: "上证指数", 3: "3388.81"})),
            _line("sh510300", _fields({1: "沪深300ETF"})),
            _line("bj430047", _fields({1: "诺思兰德"})),
            "garbage line without quote",
        ]
    )
    rows = parse_quotes(text)
    assert set(rows) == {"600000.SH", "000001.SH", "510300.SH", "430047.BJ"}
    assert rows["000001.SH"][1] == "上证指数"


def test_parse_quotes_skips_broken_lines() -> None:
    assert parse_quotes('v_hk00700="1~腾讯";') == {}
    assert parse_quotes("") == {}


def test_client_decodes_gbk_and_skips_missing_rows() -> None:
    client = TencentClient()
    http = _FakeHTTP([_FakeResponse(_line("sh600000", _fields()))])
    client._http = http
    # 请求两只, 只回一只(实测: 停牌/退市/代码不存在时服务端不返回该行)
    rows = client.fetch_batch(["600000.SH", "000001.SZ"])
    assert set(rows) == {"600000.SH"}
    assert rows["600000.SH"][1] == "浦发银行"  # GBK 中文名正确解码
    assert http.urls[0].endswith("q=sh600000,sz000001")


def test_client_raises_on_network_error() -> None:
    client = TencentClient()
    client._http = _FakeHTTP([httpx.ConnectError("boom")])
    with pytest.raises(TencentError):
        client.fetch_batch(["600000.SH"])


def test_client_raises_on_non_200() -> None:
    client = TencentClient()
    client._http = _FakeHTTP([_FakeResponse(_line("sh600000", _fields()), 403)])
    with pytest.raises(TencentError):
        client.fetch_batch(["600000.SH"])


def test_client_raises_on_broken_structure() -> None:
    client = TencentClient()
    client._http = _FakeHTTP([_FakeResponse("<html>blocked</html>")])
    with pytest.raises(TencentError):
        client.fetch_batch(["600000.SH"])


def test_client_fetch_isolates_failed_batches() -> None:
    client = TencentClient(batch_size=1, interval_s=0)
    client._http = _FakeHTTP(
        [_FakeResponse(_line("sh600000", _fields())), httpx.ConnectError("boom")]
    )
    rows = client.fetch(["600000.SH", "000001.SZ"])
    assert set(rows) == {"600000.SH"}


def test_client_fetch_raises_when_all_batches_fail() -> None:
    client = TencentClient(batch_size=1, interval_s=0)
    client._http = _FakeHTTP([httpx.ConnectError("a"), httpx.ConnectError("b")])
    with pytest.raises(TencentError):
        client.fetch(["600000.SH", "000001.SZ"])


def test_client_fetch_dedupes_symbols() -> None:
    client = TencentClient()
    http = _FakeHTTP([_FakeResponse(_line("sh600000", _fields()))])
    client._http = http
    client.fetch(["600000.SH", "600000.SH"])
    assert http.urls[0].count("sh600000") == 1


# ===== 字段映射与单位换算 =====


def test_map_row_units_and_fields() -> None:
    record = _map_row("600000.SH", _fields(), 0)
    assert record is not None
    assert record["symbol"] == "600000.SH"
    assert record["name"] == "浦发银行"
    assert record["last_price"] == pytest.approx(10.06)
    assert record["prev_close"] == pytest.approx(10.02)
    assert record["open"] == pytest.approx(9.98)
    assert record["high"] == pytest.approx(10.10)
    assert record["low"] == pytest.approx(9.95)
    assert record["volume"] == 1164223  # 手, 零换算
    assert record["amount"] == pytest.approx(11697.44 * 10_000.0)  # 万元 → 元
    assert record["change_amount"] == pytest.approx(0.04)
    assert record["change_pct"] == pytest.approx(0.004)  # 百分数 → 小数制
    assert record["turnover_rate"] == pytest.approx(0.0123)
    assert record["amplitude"] == pytest.approx(0.015)
    assert record["timestamp"] == _EXPECTED_TS
    assert record["session"] is None


def test_map_row_has_all_contract_keys() -> None:
    record = _map_row("600000.SH", _fields(), 0)
    assert record is not None
    assert set(record) == {
        "symbol", "name", "last_price", "prev_close", "open", "high", "low",
        "volume", "amount", "change_pct", "change_amount", "amplitude",
        "turnover_rate", "timestamp", "session",
    }


def test_map_row_halted_returns_none_instead_of_zero() -> None:
    """停牌/无成交(今开 0 且成交量 0): 相关字段 None, 不用 0 冒充。"""
    fields = _fields({5: "0.00", 6: "0", 33: "0.00", 34: "0.00", 36: "0", 37: "0.00"})
    record = _map_row("600000.SH", fields, 0)
    assert record is not None
    assert record["open"] is None
    assert record["high"] is None
    assert record["low"] is None
    assert record["volume"] is None
    assert record["amount"] is None
    assert record["change_pct"] is None
    assert record["change_amount"] is None
    assert record["amplitude"] is None
    assert record["turnover_rate"] is None
    # 停牌期间昨收/最新价仍是有效参考价, 保留
    assert record["last_price"] == pytest.approx(10.06)
    assert record["prev_close"] == pytest.approx(10.02)


def test_map_row_returns_none_without_any_price() -> None:
    assert _map_row("600000.SH", _fields({3: "0.00", 4: "0.00"}), 0) is None


def test_map_row_derives_change_pct_when_server_field_missing() -> None:
    record = _map_row("600000.SH", _fields({31: "", 32: ""}), 0)
    assert record is not None
    assert record["change_amount"] == pytest.approx(0.04)
    assert record["change_pct"] == pytest.approx(0.04 / 10.02)


def test_map_row_falls_back_to_local_timestamp() -> None:
    record = _map_row("600000.SH", _fields({30: ""}), 1700000000000)
    assert record is not None
    assert record["timestamp"] == 1700000000000


# ===== 五档解析 =====


def test_depth_entry_parses_five_levels() -> None:
    entry = _depth_entry(_fields(), 0)
    assert entry is not None
    assert entry["bid_prices"] == pytest.approx([10.05, 10.04, 10.03, 10.02, 10.01])
    assert entry["ask_prices"] == pytest.approx([10.07, 10.08, 10.09, 10.10, 10.11])
    assert entry["bid_volumes"] == [1000, 2000, 3000, 4000, 5000]
    assert entry["ask_volumes"] == [900, 1800, 2700, 3600, 4500]
    assert entry["timestamp"] == _EXPECTED_TS


def test_depth_entry_strips_zero_price_levels() -> None:
    """价格为 0 的档位剔除(该档无挂单), 不伪造。"""
    entry = _depth_entry(_fields({15: "0.00", 16: "0", 17: "0.00", 18: "0"}), 0)
    assert entry is not None
    assert entry["bid_prices"] == pytest.approx([10.05, 10.04, 10.03])
    assert entry["bid_volumes"] == [1000, 2000, 3000]


def test_depth_entry_omitted_when_one_side_empty() -> None:
    """一字涨停(卖盘全空)不提供盘口, 而不是返回 5 个 0。"""
    fields = _fields({19: "0.00", 21: "0.00", 23: "0.00", 25: "0.00", 27: "0.00"})
    assert _depth_entry(fields, 0) is None


def test_get_depth_batch_returns_dict_keyed_by_symbol() -> None:
    provider = _provider(
        {
            "600000.SH": _fields(),
            "000001.SZ": _fields({1: "平安银行", 3: "11.50", 4: "11.40"}),
        }
    )
    depth = provider.get_depth_batch(["600000.SH", "000001.SZ"])
    assert set(depth) == {"600000.SH", "000001.SZ"}
    assert len(depth["600000.SH"]["bid_prices"]) == 5
    assert set(depth["600000.SH"]) == {
        "bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "timestamp",
    }


def test_get_depth_batch_soft_fails() -> None:
    provider = _provider(error=TencentError("boom"))
    assert provider.get_depth_batch(["600000.SH"]) == {}
    assert provider.get_depth_batch([]) == {}


# ===== realtime =====


def test_get_realtime_maps_universe(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.plugins.tencent.provider._universe_symbols", lambda: ["600000.SH"]
    )
    provider = _provider({"600000.SH": _fields()})
    records = provider.get_realtime()
    assert len(records) == 1
    assert records[0]["symbol"] == "600000.SH"
    assert records[0]["change_pct"] == pytest.approx(0.004)


def test_get_realtime_soft_fails_on_client_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.plugins.tencent.provider._universe_symbols", lambda: ["600000.SH"]
    )
    provider = _provider(error=TencentError("boom"))
    assert provider.get_realtime() == []


def test_get_realtime_skips_empty_universe(monkeypatch) -> None:
    monkeypatch.setattr("app.plugins.tencent.provider._universe_symbols", lambda: [])
    fake = _FakeTencentClient({"600000.SH": _fields()})
    provider = TencentProvider()
    provider._client = fake
    assert provider.get_realtime() == []
    assert fake.calls == []


def test_get_realtime_drops_rows_without_price(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.plugins.tencent.provider._universe_symbols",
        lambda: ["600000.SH", "000001.SZ"],
    )
    provider = _provider(
        {
            "600000.SH": _fields(),
            "000001.SZ": _fields({3: "0.00", 4: "0.00"}),
        }
    )
    records = provider.get_realtime()
    assert [r["symbol"] for r in records] == ["600000.SH"]


def test_get_realtime_soft_fails_on_universe_error(monkeypatch) -> None:
    def _boom() -> list[str]:
        raise RuntimeError("维表炸了")

    monkeypatch.setattr("app.plugins.tencent.provider._universe_symbols", _boom)
    assert _provider({"600000.SH": _fields()}).get_realtime() == []


# ===== 指数 =====


def test_get_realtime_indices_maps_and_dedupes() -> None:
    provider = _provider(
        {
            "000001.SH": _fields({1: "上证指数", 3: "3388.81", 4: "3370.00"}),
            "399001.SZ": _fields({1: "深证成指", 3: "10800.10", 4: "10750.00"}),
            "399006.SZ": _fields({1: "创业板指", 3: "2200.55", 4: "2190.00"}),
        }
    )
    records = provider.get_realtime_indices(
        ["000001.SH", "000001.SH", "399001.SZ", "399006.SZ"]
    )
    assert records is not None
    assert [r["symbol"] for r in records] == ["000001.SH", "399001.SZ", "399006.SZ"]
    assert records[0]["name"] == "上证指数"
    assert records[0]["last_price"] == pytest.approx(3388.81)
    assert records[0]["change_pct"] == pytest.approx(0.004)


def test_get_realtime_indices_returns_none_on_error() -> None:
    """失败返回 None(上层保留上一轮缓存), 不是空列表。"""
    provider = _provider(error=TencentError("boom"))
    assert provider.get_realtime_indices(["000001.SH"]) is None


def test_get_realtime_indices_empty_symbols() -> None:
    provider = _provider()
    assert provider.get_realtime_indices([]) == []


def test_get_realtime_indices_success_without_data_returns_empty() -> None:
    provider = _provider({})
    assert provider.get_realtime_indices(["000001.SH"]) == []


# ===== 能力声明 / 生命周期 / 试拉 =====


def test_capabilities_declared() -> None:
    provider = TencentProvider()
    assert provider.name == "tencent"
    assert provider.builtin is True
    assert set(provider.config.datasets) == {"realtime", "depth5"}


def test_availability_never_raises() -> None:
    available, reason = availability()
    assert available is True
    assert isinstance(reason, str)


def test_close_closes_client() -> None:
    provider = TencentProvider()
    fake = _FakeTencentClient()
    provider._client = fake
    provider.close()
    assert fake.closed is True
    assert provider._client is None
    provider.close()  # 重复 close 不抛


def test_test_dataset_realtime() -> None:
    provider = _provider({"600000.SH": _fields()})
    result = provider.test_dataset("realtime", ["600000.SH"])
    assert result["provider"] == "tencent"
    assert result["dataset"] == "realtime"
    assert result["rows"] == 1
    assert result["columns"][0] == "symbol"
    assert result["preview"][0]["symbol"] == "600000.SH"


def test_test_dataset_depth5() -> None:
    provider = _provider({"600000.SH": _fields()})
    result = provider.test_dataset("depth5", ["600000.SH"])
    assert result["rows"] == 1
    assert "bid_prices" in result["columns"]
    assert result["preview"][0]["bid_prices"][0] == pytest.approx(10.05)


def test_test_dataset_reports_client_error() -> None:
    provider = _provider(error=TencentError("boom"))
    result = provider.test_dataset("realtime", ["600000.SH"])
    assert result["rows"] == 0
    assert "boom" in result["error"]


def test_test_dataset_unsupported() -> None:
    result = TencentProvider().test_dataset("daily")
    assert result["rows"] == 0
    assert "daily" in result["error"]


# ===== 全市场标的池 =====


def test_universe_symbols_merges_dimensions(monkeypatch, tmp_path) -> None:
    (tmp_path / "instruments").mkdir()
    pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"]}).write_parquet(
        tmp_path / "instruments" / "instruments.parquet"
    )
    (tmp_path / "instruments_etf").mkdir()
    pl.DataFrame({"symbol": ["510300.SH"]}).write_parquet(
        tmp_path / "instruments_etf" / "etf.parquet"
    )
    (tmp_path / "instruments_index").mkdir()
    pl.DataFrame(
        {
            "symbol": ["000001.SH", "399001.SZ", "159915.SZ"],
            "asset_type": ["index", "index", "etf"],
        }
    ).write_parquet(tmp_path / "instruments_index" / "index.parquet")
    (tmp_path / "user_data").mkdir()
    pl.DataFrame({"symbol": ["600519.SH", "000001.SH"]}).write_parquet(
        tmp_path / "user_data" / "watchlist.parquet"
    )

    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    symbols = _universe_symbols()
    # 个股维表 ∪ ETF 维表 ∪ 自选, 指数(000001.SH/399001.SZ)剔除,
    # instruments_index 里 asset_type=etf 的 legacy 行保留
    assert symbols == ["000001.SZ", "159915.SZ", "510300.SH", "600000.SH", "600519.SH"]


def test_universe_symbols_empty_when_no_dimensions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    assert _universe_symbols() == []
