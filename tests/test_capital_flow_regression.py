"""Regression coverage for stock identity, monetary units and budget starvation."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager
from data_provider.fundamental_adapter import AkshareFundamentalAdapter, _parse_stock_flow_history


def history():
    return pd.DataFrame({
        "日期": pd.date_range("2026-09-17", periods=10, freq="B"),
        "主力净流入-净额": [1., 2., 3., 4., 5., 6., 7., 8., 9., 10.],
        "主力净流入-净占比": [99.] * 10,
    })


@pytest.mark.parametrize("code,market", [("601328", "sh"), ("000001", "sz"), ("920001", "bj")])
def test_requested_code_and_market_without_unscoped_fallback(code, market):
    endpoint = Mock(return_value=history())
    sector = Mock(side_effect=AssertionError("sector must not block stock result"))
    fake_ak = SimpleNamespace(stock_individual_fund_flow=endpoint, stock_sector_fund_flow_rank=sector)
    with patch.dict("sys.modules", {"akshare": fake_ak}), \
            patch("data_provider.eastmoney_history.stock_flow_history", endpoint):
        result = AkshareFundamentalAdapter().get_capital_flow(code, include_sector=False)
    endpoint.assert_called_once_with(stock=code, market=market)
    sector.assert_not_called()
    assert result["stock_flow"]["main_net_inflow"] == 10
    assert result["stock_flow"]["inflow_5d"] == 40
    assert result["stock_flow"]["inflow_10d"] == 55
    assert result["stock_flow"]["data_date"] == "2026-09-30"


def test_latest_date_not_first_row_and_zero_is_valid():
    df = history()
    df.loc[9, "主力净流入-净额"] = 0
    result = _parse_stock_flow_history(df.iloc[::-1], "601328")
    assert result["main_net_inflow"] == 0
    assert result["inflow_5d"] == 30


def test_short_history_does_not_fabricate_five_day_total():
    result = _parse_stock_flow_history(history().tail(2), "601328")
    assert result["inflow_5d"] is None
    assert result["inflow_10d"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_invalid_latest_amount_is_not_replaced_by_an_older_value(bad):
    df = history()
    df.loc[9, "主力净流入-净额"] = bad
    with pytest.raises(ValueError, match="invalid_latest"):
        _parse_stock_flow_history(df, "601328")


def test_reject_ratio_only_or_wrong_stock_frames():
    with pytest.raises(ValueError):
        _parse_stock_flow_history(history().drop(columns="主力净流入-净额"), "601328")
    df = history().assign(代码="000001")
    with pytest.raises(ValueError):
        _parse_stock_flow_history(df, "601328")


def test_endpoint_failure_is_explicit_and_never_calls_default_stock():
    endpoint = Mock(side_effect=ConnectionError("unavailable"))
    with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_individual_fund_flow=endpoint)}), \
            patch("data_provider.eastmoney_history.stock_flow_history", endpoint):
        result = AkshareFundamentalAdapter().get_capital_flow("000001", include_sector=False)
    assert endpoint.call_count == 1
    assert result["status"] == "failed"
    assert result["stock_flow"] == {}
    assert any("ConnectionError" in error for error in result["errors"])


def test_stock_flow_runs_before_slow_valuation_exhausts_stage():
    manager = DataFetcherManager(fetchers=[])
    config = SimpleNamespace(enable_fundamental_pipeline=True, fundamental_cache_ttl_seconds=0,
                             fundamental_stage_timeout_seconds=8., fundamental_fetch_timeout_seconds=8.,
                             fundamental_retry_max=0)
    calls = []

    def run(fn, timeout, label):
        calls.append(label)
        if label == "capital_flow":
            return {"stock_flow": {"main_net_inflow": 100.}, "status": "partial"}, None, 1
        return None, "simulated slow quote", 8000

    with patch("src.config.get_config", return_value=config), patch.object(manager, "_run_with_retry", side_effect=run):
        result = manager.get_fundamental_context("601328")
    assert calls == ["capital_flow", "fundamental_valuation"]
    assert result["capital_flow"]["data"]["stock_flow"]["main_net_inflow"] == 100
    assert result["growth"]["status"] == "failed"


def test_zero_budget_never_starts_provider():
    manager = DataFetcherManager(fetchers=[])
    with patch.object(manager, "_run_with_retry") as run:
        result = manager.get_capital_flow_context("601328", budget_seconds=0)
    run.assert_not_called()
    assert result["status"] == "failed"
    assert "fundamental stage timeout" in result["errors"]
