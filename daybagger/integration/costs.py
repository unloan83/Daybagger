from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal
    total: Decimal

    def total_bps(self, buy_turnover: Decimal, sell_turnover: Decimal) -> float:
        avg = (buy_turnover + sell_turnover) / Decimal("2")
        if avg <= 0:
            raise ValueError("turnover must be positive")
        return float(self.total / avg * Decimal("10000"))


class IndiaEquityIntradayCostModel:
    """
    NSE cash intraday cost schedule verified against Upstox pricing on 2026-09-02.

    Rates are explicit and isolated here so they can be updated without touching
    strategy/model code.
    """
    def __init__(
        self,
        *,
        brokerage_rate: Decimal = Decimal("0.001"),      # 0.1%
        brokerage_cap_per_order: Decimal = Decimal("20"),
        stt_sell_rate: Decimal = Decimal("0.00025"),     # 0.025%
        nse_transaction_rate: Decimal = Decimal("0.0000307"), # 0.00307%
        sebi_rate: Decimal = Decimal("0.000001"),        # ₹10/crore
        stamp_buy_rate: Decimal = Decimal("0.00003"),    # 0.003%
        gst_rate: Decimal = Decimal("0.18"),
    ):
        self.brokerage_rate = brokerage_rate
        self.brokerage_cap_per_order = brokerage_cap_per_order
        self.stt_sell_rate = stt_sell_rate
        self.nse_transaction_rate = nse_transaction_rate
        self.sebi_rate = sebi_rate
        self.stamp_buy_rate = stamp_buy_rate
        self.gst_rate = gst_rate

    def estimate_round_trip(
        self,
        *,
        buy_turnover: Decimal,
        sell_turnover: Decimal,
    ) -> CostBreakdown:
        if buy_turnover <= 0 or sell_turnover <= 0:
            raise ValueError("buy/sell turnover must be positive")

        buy_brokerage = min(
            self.brokerage_cap_per_order,
            buy_turnover * self.brokerage_rate,
        )
        sell_brokerage = min(
            self.brokerage_cap_per_order,
            sell_turnover * self.brokerage_rate,
        )
        brokerage = buy_brokerage + sell_brokerage
        stt = sell_turnover * self.stt_sell_rate
        exchange = (buy_turnover + sell_turnover) * self.nse_transaction_rate
        sebi = (buy_turnover + sell_turnover) * self.sebi_rate
        stamp = buy_turnover * self.stamp_buy_rate
        gst = (brokerage + exchange + sebi) * self.gst_rate
        total = brokerage + stt + exchange + sebi + stamp + gst

        return CostBreakdown(
            brokerage=_money(brokerage),
            stt=_money(stt),
            exchange=_money(exchange),
            sebi=_money(sebi),
            stamp=_money(stamp),
            gst=_money(gst),
            total=_money(total),
        )

    def round_trip_bps_for_notional(self, notional_inr: Decimal) -> float:
        """Known statutory/brokerage cost in bps for equal buy/sell notional."""
        if notional_inr <= 0:
            raise ValueError("notional_inr must be positive")
        costs = self.estimate_round_trip(
            buy_turnover=notional_inr,
            sell_turnover=notional_inr,
        )
        return costs.total_bps(notional_inr, notional_inr)

    def conservative_linear_round_trip_bps(self) -> float:
        """
        Maximum normal percentage-cost regime before the brokerage cap helps.

        A small positive notional remains below the per-order brokerage cap, so
        this is conservative for larger trades under the same published schedule.
        """
        return self.round_trip_bps_for_notional(Decimal("1000"))


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
