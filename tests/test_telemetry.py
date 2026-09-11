import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock
from trading_contracts.telemetry import send_telegram
from daybagger.engine.telemetry_daemon import check_systemd_status, get_ledger_stats
from daybagger.engine.order_desk import PaperOrderDesk
from daybagger.engine.risk_gate import RiskDesk
from daybagger.engine.risk_metadata import InstrumentRiskMetadata
from trading_contracts.schemas.v1 import Direction, MarketRegime, SignalCandidate

class TestTelemetry(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_send_telegram_missing_credentials(self):
        # OCI has credential fallback files. Isolate both the loader and network
        # so this unit test can never send an external message.
        with patch("trading_contracts.telemetry._load_env"), patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            self.assertFalse(send_telegram("Test message"))
            mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "mock_token", "TELEGRAM_CHAT_ID": "mock_chat"}, clear=True)
    def test_send_telegram_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = send_telegram("Hello World")
        self.assertTrue(res)

    @patch("subprocess.run")
    def test_check_systemd_status(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "active\n"
        mock_run.return_value = mock_res

        is_active, status = check_systemd_status("dualengine.service")
        self.assertTrue(is_active)
        self.assertEqual(status, "active")

    def test_ledger_stats_report_gross_cost_slippage_and_net_separately(self):
        metadata = InstrumentRiskMetadata.from_profiles({
            "INFY": ("INE009A01021", "IT - Software"),
        })
        with TemporaryDirectory() as tmp_dir:
            ledger_path = str(Path(tmp_dir) / "telemetry.duckdb")
            desk = PaperOrderDesk(db_path=ledger_path, risk_metadata=metadata)
            now = datetime.now(timezone.utc)
            signal = SignalCandidate(
                signal_id="telemetry-trade",
                instrument_id="INFY",
                created_at=now,
                valid_until=now,
                direction=Direction.LONG,
                setup_type="CPR_OI_INTELLIGENCE",
                regime=MarketRegime.TRENDING,
                confidence=0.9,
                entry_trigger=1500.0,
                invalidation_level=1490.0,
                reason_codes=["TEST"],
            )
            evaluation = RiskDesk().evaluate(signal)
            assert desk.record_signal(signal, evaluation) == "OPEN"
            desk.evaluate_open_positions(
                {"INFY": evaluation.target},
                now,
            )
            with patch("daybagger.engine.telemetry_daemon.LEDGER_PATH", ledger_path):
                stats = get_ledger_stats()
            self.assertEqual(stats["closed"], 1)
            self.assertGreater(stats["gross_pnl"], stats["net_pnl"])
            self.assertGreater(stats["modeled_costs"], 0)
            self.assertGreater(stats["modeled_slippage"], 0)
            self.assertAlmostEqual(
                stats["net_pnl"],
                stats["gross_pnl"]
                - stats["modeled_costs"]
                - stats["modeled_slippage"],
                places=2,
            )

if __name__ == "__main__":
    unittest.main()
