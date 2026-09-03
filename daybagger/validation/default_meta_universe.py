"""Locked diversified research universe for the first Daybagger meta model.

This is a research cohort, NOT the live trading universe. Live paper runtime still
broad-scans the complete official NSE MIS equity universe before the deep scan.
The cohort is fixed before validation so post-result symbol cherry-picking is not
possible.
"""

DEFAULT_META_VALIDATION_SYMBOLS: tuple[str, ...] = (
    "ADANIPORTS",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJFINSV",
    "BAJFINANCE",
    "BHARTIARTL",
    "CIPLA",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HINDUNILVR",
    "ICICIBANK",
    "INFY",
    "ITC",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "MARUTI",
    "M&M",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBIN",
    "SUNPHARMA",
    "TATASTEEL",
    "TCS",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
)
