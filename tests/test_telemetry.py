import unittest
from unittest.mock import patch, MagicMock
from trading_contracts.telemetry import send_telegram
from daybagger.engine.telemetry_daemon import check_systemd_status, get_ledger_stats

class TestTelemetry(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_send_telegram_missing_credentials(self):
        self.assertFalse(send_telegram("Test message"))

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

if __name__ == "__main__":
    unittest.main()
