from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecialistFamily:
    """
    Research family only. Required features define the information lens.
    Coefficients/thresholds are NOT embedded here; they must be learned and validated.
    """
    family_id: str
    purpose: str
    required_features: tuple[str, ...]


SPECIALIST_FAMILIES: dict[str, SpecialistFamily] = {
    "relative_strength": SpecialistFamily(
        family_id="relative_strength",
        purpose="Cross-sectional stock leadership/weakness versus NIFTY and sector.",
        required_features=(
            "rs_vs_benchmark_bps",
            "rs_vs_sector_bps",
            "sector_session_return_percentile",
            "stock_session_return_bps",
            "market_session_return_bps",
        ),
    ),
    "trend_pullback": SpecialistFamily(
        family_id="trend_pullback",
        purpose="Trend persistence plus controlled return toward session VWAP/structure.",
        required_features=(
            "stock_session_return_bps",
            "stock_return_5m_bps",
            "stock_return_15m_bps",
            "stock_vwap_distance_bps",
            "stock_trend_efficiency",
            "market_trend_efficiency",
        ),
    ),
    "volume_participation": SpecialistFamily(
        family_id="volume_participation",
        purpose="Time-normalised participation combined with price response.",
        required_features=(
            "relative_volume",
            "stock_return_5m_bps",
            "stock_return_15m_bps",
            "stock_session_return_bps",
            "breadth_advance_ratio",
        ),
    ),
    "microstructure": SpecialistFamily(
        family_id="microstructure",
        purpose="Executable spread and visible buy/sell quantity pressure.",
        required_features=(
            "spread_bps",
            "buy_sell_quantity_imbalance",
            "stock_return_5m_bps",
            "stock_vwap_distance_bps",
        ),
    ),
    "catalyst": SpecialistFamily(
        family_id="catalyst",
        purpose="News/event continuation or reversal when verified catalyst evidence exists.",
        required_features=(
            "catalyst_direction",
            "catalyst_recency_minutes",
            "catalyst_quality",
            "relative_volume",
            "stock_return_5m_bps",
            "rs_vs_sector_bps",
        ),
    ),
}
