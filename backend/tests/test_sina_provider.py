"""新浪行情插件契约测试。

不依赖真实网络与 Key:
  - client 层用 httpx.MockTransport 验证代码转换、请求参数与响应解包语义;
  - provider 层注入假客户端, 验证字段映射与股→手换算、datetime 解析与 naive 墙钟、
    freq→scale 映射、空数据、软失败、进度回调与能力声明;
  - plugin.yaml 清单与 loader 的 entry/check 引用可被真实解析。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest

from app.data_providers.custom import loader
from app.plugins.sina.client import MAX_DATALEN, SinaClient, SinaError, to_sina_symbol
from app.plugins.sina.provider import (
    SinaProvider,
    _beijing_naive,
    _datalen_for_window,
    _minute_rows,
    _parse_dt,
    _shares_to_lots,
    availability,
)

_BEIJING = ZoneInfo("Asia/Shanghai")
_TODAY = datetime.now(_BEIJING).date()
_YESTERDAY = _TODAY - timedelta(days=1)
_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


# ===== 构造上游实测形状的行 =====


def _bar(day: str, close: str = "10.070", volume: str = "371900") -> dict:
    """上游 1m 数组的一行 (字段均为字符串, volume 单位股, amount 单位元)。"""
    return {
        "day": day,
        "open": "10.050",
        "high": "10.080",
        "low": "10.040",
        "close": close,
        "volume": volume,
        "amount": "3742919.0377",
    }


def _day_at(day, hhmm: str) -> str:
    return f"{day.isoformat()} {hhmm}"


class _FakeClient:
    """假 SinaClient: 记录调用并按标的返回行 / 抛错。"""

    def __init__(self, bars: dict | None = None, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self._bars = bars or {}
        self._fail = set(fail)
        self.closed = False

    def minute_bars(self, symbol: str, scale: int = 1, datalen: int = MAX_DATALEN):
        self.calls.append((symbol, scale, datalen))
        if symbol in self._fail:
            raise SinaError(f"boom: {symbol}")
        return list(self._bars.get(symbol, []))

    def close(self) -> None:
        self.closed = True


def _provider(fake: _FakeClient) -> SinaProvider:
    provider = SinaProvider()
    provider._client = fake
    return provider


def _client_with(handler) -> SinaClient:
    """把 httpx 传输层换成 MockTransport, 不触网; 保留生产客户端的请求头(含 UA)。"""
    client = SinaClient()
    headers = dict(client._http.headers)
    client._http.close()
    client._http = httpx.Client(headers=headers, transport=httpx.MockTransport(handler))
    return client


# ===== client 层: 代码转换 =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600000.SH", "sh600000"),
        ("000001.SZ", "sz000001"),
        ("920001.BJ", "bj920001"),
        ("sh600000", "sh600000"),
        ("600000", "sh600000"),  # 裸码兜底: 6 开头 → 沪
        ("000001", "sz000001"),
        ("300750", "sz300750"),
        ("510300", "sh510300"),
        ("920001", "bj920001"),  # 920 段归北交所
        ("430047", "bj430047"),
        ("", ""),
        ("600000.HK", ""),
        ("ABCDEF", ""),
    ],
)
def test_to_sina_symbol(raw: str, expected: str) -> None:
    assert to_sina_symbol(raw) == expected


# ===== client 层: 请求与解包 =====


def test_client_sends_required_params_and_unwraps_array() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json=[_bar("2026-11-02 14:09:00")])

    client = _client_with(handler)
    try:
        bars = client.minute_bars("600000.SH", scale=1, datalen=99_999)
    finally:
        client.close()

    params = seen["params"]
    assert params["symbol"] == "sh600000"
    assert params["scale"] == "1"
    assert params["ma"] == "no"
    # datalen 必须封顶, 上游对过大值直接回 null
    assert params["datalen"] == str(MAX_DATALEN)
    assert "Mozilla" in str(seen["ua"])  # 无 UA 会被上游 403
    assert bars and bars[0]["close"] == "10.070"


def test_client_null_payload_is_empty_not_error() -> None:
    client = _client_with(lambda request: httpx.Response(200, text="null"))
    try:
        assert client.minute_bars("600000.SH") == []
    finally:
        client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"code": 0}),  # 不是数组 → 结构异常
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
def test_client_errors_raise_sina_error(response: httpx.Response) -> None:
    client = _client_with(lambda request: response)
    try:
        with pytest.raises(SinaError):
            client.minute_bars("600000.SH")
    finally:
        client.close()


def test_client_rejects_unknown_symbol_and_scale() -> None:
    client = _client_with(lambda request: httpx.Response(200, json=[]))
    try:
        with pytest.raises(SinaError):
            client.minute_bars("600000.HK")
        with pytest.raises(SinaError):
            client.minute_bars("600000.SH", scale=2)
    finally:
        client.close()


# ===== provider 层: 字段映射与单位换算 =====


def test_minute_field_mapping_and_lot_conversion() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})
    df = _provider(fake).get_minute(
        ["600000.SH"], datetime(2026, 11, 2), datetime(2026, 11, 2, 23, 59, 59)
    )

    assert df.columns == _COLUMNS
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600000.SH"
    # 价格: 原始不复权值, 字符串 → float
    assert (row["open"], row["high"], row["low"], row["close"]) == (
        10.050, 10.080, 10.040, 10.070,
    )
    # volume: 上游「股」→ 内部「手」(÷100)
    assert row["volume"] == pytest.approx(3719.0)
    # amount: 上游「元」→ 原样透传
    assert row["amount"] == pytest.approx(3742919.0377)


def test_lot_conversion_floors_and_never_fakes() -> None:
    assert _shares_to_lots("371900") == 3719.0
    assert _shares_to_lots("371999") == 3719.0  # 不足一手的零股向下取整
    assert _shares_to_lots("99") == 0.0
    assert _shares_to_lots(None) is None  # 缺失不伪造 0
    assert _shares_to_lots("abc") is None


# ===== provider 层: datetime 契约 =====


def test_minute_datetime_is_naive_beijing_wallclock() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})
    df = _provider(fake).get_minute(["600000.SH"], None, None)

    assert df.schema["datetime"] == pl.Datetime("us")
    assert df.schema["datetime"].time_zone is None  # 硬契约: 无时区
    value = df["datetime"][0]
    assert value == datetime(2026, 11, 2, 9, 31, 0)
    assert value.tzinfo is None  # naive 北京墙钟, 不做时区换算


def test_parse_dt_and_beijing_naive() -> None:
    assert _parse_dt("2026-11-02 14:09:00") == datetime(2026, 11, 2, 14, 9, 0)
    assert _parse_dt(" 2026-11-02 14:09:00 ") == datetime(2026, 11, 2, 14, 9, 0)
    assert _parse_dt("2026-11-02") is None
    assert _parse_dt(None) is None
    # 已 naive 视为北京墙钟原样返回; 带时区的换算成北京墙钟后去 tz
    assert _beijing_naive(datetime(2026, 11, 2, 9, 31)) == datetime(2026, 11, 2, 9, 31)
    utc_aware = datetime(2026, 11, 2, 1, 31, tzinfo=ZoneInfo("UTC"))
    assert _beijing_naive(utc_aware) == datetime(2026, 11, 2, 9, 31)


# ===== provider 层: freq 映射 =====


@pytest.mark.parametrize(("freq", "scale"), [("1m", 1), ("5m", 5), ("15m", 15),
                                             ("30m", 30), ("60m", 60), ("1M", 1)])
def test_freq_maps_to_upstream_scale(freq: str, scale: int) -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})
    _provider(fake).get_minute(["600000.SH"], None, None, freq=freq)
    assert fake.calls[0][1] == scale


def test_unknown_freq_returns_empty_without_request() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})
    df = _provider(fake).get_minute(["600000.SH"], None, None, freq="3m")
    assert df.is_empty()
    assert df.columns == _COLUMNS
    assert fake.calls == []  # 未知周期不空转请求, 交上层回退


# ===== provider 层: 空数据 =====


def test_empty_upstream_yields_schema_only_frame() -> None:
    df = _provider(_FakeClient({"600000.SH": []})).get_minute(["600000.SH"], None, None)
    assert df.is_empty()
    assert df.columns == _COLUMNS
    assert df.schema["datetime"] == pl.Datetime("us")


def test_no_symbols_and_unsupported_asset_type_are_empty() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})
    provider = _provider(fake)
    assert provider.get_minute([], None, None).is_empty()
    # 未知 asset_type: 空手而归且不发请求 (白名单外的标的不空转)
    assert provider.get_minute(["600000.SH"], None, None, asset_type="warrant").is_empty()
    assert provider.get_intraday_batch([], asset_type="stock").is_empty()
    assert fake.calls == []


@pytest.mark.parametrize(
    ("symbol", "asset_type"),
    [("600000.SH", "stock"), ("510300.SH", "etf"), ("000001.SH", "index")],
)
def test_supported_asset_types_are_served(symbol: str, asset_type: str) -> None:
    """个股/ETF/指数三类标的都真发请求并出数据 (上游同布局, volume 一律为股)。"""
    fake = _FakeClient({symbol: [_bar("2026-11-02 09:31:00")]})
    df = _provider(fake).get_minute([symbol], None, None, asset_type=asset_type)
    assert df.height == 1
    assert df["volume"].to_list() == [3719.0]  # 371900 股 → 3719 手
    assert [c[0] for c in fake.calls] == [symbol]


def test_window_filters_rows() -> None:
    bars = [_bar("2026-11-02 09:31:00"), _bar("2026-11-02 09:32:00"),
            _bar("2026-11-02 09:33:00")]
    df = _provider(_FakeClient({"600000.SH": bars})).get_minute(
        ["600000.SH"], datetime(2026, 11, 2, 9, 32), datetime(2026, 11, 2, 9, 32, 59)
    )
    assert df["datetime"].to_list() == [datetime(2026, 11, 2, 9, 32)]


def test_malformed_rows_are_skipped_not_faked() -> None:
    rows = _minute_rows(
        "600000.SH",
        [
            {"day": "bad", "close": "1"},
            "not-a-dict",
            {"day": "2026-11-02 09:31:00"},  # 缺 close → 停牌/无成交, 跳过
            _bar("2026-11-02 09:32:00"),
        ],
    )
    assert [r["datetime"] for r in rows] == [datetime(2026, 11, 2, 9, 32)]


def test_datalen_scales_with_window_and_caps() -> None:
    now = datetime(2026, 11, 2, 15, 0)
    assert _datalen_for_window(None, now) == MAX_DATALEN  # 全深度直接给上限
    short = _datalen_for_window(now - timedelta(days=1), now)
    assert 2 * 238 <= short <= MAX_DATALEN
    assert _datalen_for_window(now - timedelta(days=30), now) == MAX_DATALEN


# ===== provider 层: 多标的与软失败 =====


def test_multiple_symbols_are_concatenated() -> None:
    fake = _FakeClient({
        "600000.SH": [_bar("2026-11-02 09:31:00"), _bar("2026-11-02 09:32:00")],
        "000001.SZ": [_bar("2026-11-02 09:31:00")],
    })
    df = _provider(fake).get_minute(["600000.SH", "000001.SZ"], None, None)
    assert df.height == 3
    assert sorted(set(df["symbol"].to_list())) == ["000001.SZ", "600000.SH"]


def test_get_minute_raises_so_upper_layer_can_fall_back() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]}, fail=("000001.SZ",))
    with pytest.raises(SinaError):
        _provider(fake).get_minute(["600000.SH", "000001.SZ"], None, None)


def test_intraday_batch_keeps_today_only() -> None:
    # datalen 尾部天然带前一日余量, 修复轮必须按 today_only 过滤
    bars = [_bar(_day_at(_YESTERDAY, "14:59:00")), _bar(_day_at(_TODAY, "09:31:00")),
            _bar(_day_at(_TODAY, "09:32:00"))]
    fake = _FakeClient({"600000.SH": bars, "000001.SZ": []}, fail=("000002.SZ",))
    # 契约签名与 tdx 参考实现一致: get_intraday_batch(symbols, count, asset_type),
    # 不含 on_chunk_done (服务端以 method(symbols) 单参调用)
    df = _provider(fake).get_intraday_batch(["600000.SH", "000001.SZ", "000002.SZ"])

    assert df.height == 2
    assert set(df["symbol"].to_list()) == {"600000.SH"}
    assert all(d.date() == _TODAY for d in df["datetime"].to_list())
    # 失败/空批次的标的同样占一次请求, 不会被静默跳过而不计数
    assert len(fake.calls) == 3
    assert {call[0] for call in fake.calls} == {"600000.SH", "000001.SZ", "000002.SZ"}


def test_get_minute_callback_covers_every_symbol_including_empty() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")], "000001.SZ": []})
    progress: list[tuple[int, int]] = []
    _provider(fake).get_minute(
        ["600000.SH", "000001.SZ", "000002.SZ"],
        datetime(2026, 11, 2), datetime(2026, 11, 2, 23, 59, 59),
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )
    # 每只标的都要回调一次(含空批次), 否则前端进度条卡住
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_intraday_batch_soft_failure_returns_partial_frame() -> None:
    fake = _FakeClient(
        {"600000.SH": [_bar(_day_at(_TODAY, "09:31:00"))]},
        fail=("000001.SZ", "000002.SZ"),
    )
    df = _provider(fake).get_intraday_batch(["600000.SH", "000001.SZ", "000002.SZ"])
    assert df.height == 1  # 失败标的软跳过, 不影响其余标的
    assert df["symbol"].to_list() == ["600000.SH"]


def test_intraday_batch_all_failed_returns_empty_frame() -> None:
    df = _provider(_FakeClient(fail=("600000.SH",))).get_intraday_batch(["600000.SH"])
    assert df.is_empty()
    assert df.columns == _COLUMNS


def test_progress_callback_exception_never_breaks_data() -> None:
    fake = _FakeClient({"600000.SH": [_bar("2026-11-02 09:31:00")]})

    def boom(cur: int, total: int) -> None:
        raise RuntimeError("callback bug")

    df = _provider(fake).get_minute(["600000.SH"], None, None, on_chunk_done=boom)
    assert df.height == 1  # 回调是装饰性副作用


# ===== provider 层: 能力声明与生命周期 =====


def test_capability_declaration() -> None:
    provider = SinaProvider()
    assert provider.name == "sina"
    assert provider.builtin is True
    assert provider.minute_history_days == 4  # 浅源: 1023 根 1m ≈ 4.3 交易日
    assert set(provider.config.datasets) == {"minute", "full_minute"}
    assert availability() == (True, "")  # 免费无 Key, 恒可用且不抛
    # 新浪无全市场批量分钟端点 → 故意不实现, 服务据此降级为仅修复轮
    assert not hasattr(provider, "get_intraday_latest")


def test_close_releases_client() -> None:
    fake = _FakeClient()
    provider = _provider(fake)
    provider.close()
    assert fake.closed is True
    assert provider._client is None
    provider.close()  # 幂等


def test_test_dataset_reports_rows_and_is_json_safe() -> None:
    bars = [_bar(_day_at(_TODAY, "09:31:00")), _bar(_day_at(_TODAY, "09:32:00"))]
    provider = _provider(_FakeClient({"600000.SH": bars}))

    minute = provider.test_dataset("minute", ["600000.SH"])
    assert minute["provider"] == "sina"
    assert minute["dataset"] == "minute"
    assert minute["rows"] == 2
    assert isinstance(minute["preview"][0]["datetime"], str)  # datetime → ISO 字符串

    full = provider.test_dataset("full_minute", ["600000.SH"])
    assert full["rows"] == 2

    unsupported = provider.test_dataset("daily")
    assert unsupported["rows"] == 0
    assert "error" in unsupported


def test_test_dataset_reports_error_instead_of_raising() -> None:
    provider = _provider(_FakeClient(fail=("600000.SH",)))
    result = provider.test_dataset("minute", ["600000.SH"])
    assert result["rows"] == 0
    assert "boom" in result["error"]


# ===== 插件清单与 loader 引用 =====


def test_plugin_manifest_contract() -> None:
    manifest = loader.plugin_manifest("sina")
    assert manifest is not None
    assert manifest["name"] == "sina"
    assert manifest["display_name"] == "新浪行情 (免费)"
    assert manifest["runtime"] == "none"
    assert manifest["entry"] == "app.plugins.sina.provider:SinaProvider"
    assert manifest["check"] == "app.plugins.sina.provider:availability"
    assert list(manifest["datasets"]) == ["minute", "full_minute"]
    # 免费源: 不得声明 Key 环境变量, 否则设置页会要求用户填 Key
    assert not manifest.get("api_key_env")
    assert "免费" in manifest["description"]
    notes = list(manifest.get("usage_notes") or [])
    assert any("交易日" in note for note in notes)  # 必须写明历史深度
    assert any("全市场" in note for note in notes)  # 必须写明全量分钟耗时量级


def test_loader_entry_and_check_resolve() -> None:
    manifest = loader.plugin_manifest("sina")
    assert loader._load_entry(manifest["entry"]) is SinaProvider
    assert loader._call_check(manifest["check"]) == (True, "")
