from decimal import Decimal

from daybagger.data.universe import (
    NSEEquityUniverse,
    ObservableEquity,
    usable_for_execution,
)


BOD = [
    {
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE001",
        "trading_symbol": "AAA",
        "name": "AAA LIMITED",
        "isin": "INE001",
        "tick_size": 5.0,
        "security_type": "NORMAL",
        "cas_eligible": True,
    },
    {
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE002",
        "trading_symbol": "BBB",
        "name": "BBB LIMITED",
        "isin": "INE002",
        "tick_size": 5.0,
        "security_type": "NORMAL",
        "cas_eligible": False,
    },
    {
        "segment": "NSE_INDEX",
        "exchange": "NSE",
        "instrument_type": "INDEX",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "trading_symbol": "NIFTY",
        "name": "NIFTY 50",
        "isin": "",
        "tick_size": 5.0,
    },
]

MIS = [
    {
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE002",
    },
    {
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "instrument_key": "NSE_EQ|INE999",
    },
]


def transport(url, timeout):
    return MIS if "NSE_MIS" in url else BOD


def test_universe_is_official_bod_intersection_mis() -> None:
    items = NSEEquityUniverse(transport=transport).load_mis_equities()
    assert len(items) == 1
    assert items[0].trading_symbol == "BBB"
    assert items[0].instrument_key == "NSE_EQ|INE002"


def test_no_strategy_filter_is_hidden_in_universe() -> None:
    items = NSEEquityUniverse(transport=transport).load_mis_equities()
    item = items[0]
    assert item.name == "BBB LIMITED"
    assert item.cas_eligible is False
    assert item.tick_size == Decimal("5.0")
