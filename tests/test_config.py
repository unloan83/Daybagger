from pathlib import Path
import pytest
from daybagger.config import load_settings
from daybagger.errors import ConfigurationError
BASE="""
[app]
name="Daybagger"
environment="test"
trading_mode="{mode}"
timezone="Asia/Kolkata"
log_level="INFO"
[capital]
starting_capital_inr=30000.0
[storage]
control_db_path="data/test.sqlite3"
[execution]
max_quote_age_seconds=15
paper_slippage_bps=0.0
"""
def test_paper_mode_is_allowed(tmp_path:Path):
    p=tmp_path/'c.toml'; p.write_text(BASE.format(mode='paper')); assert load_settings(p).app.trading_mode=='paper'
def test_live_mode_is_blocked(tmp_path:Path):
    p=tmp_path/'c.toml'; p.write_text(BASE.format(mode='live'));
    with pytest.raises(ConfigurationError): load_settings(p)
