from pathlib import Path
import pytest
from daybagger.bootstrap import verify_golden_rules
from daybagger.errors import GoldenRulesError

def test_missing_goldenrules_fails_closed(tmp_path:Path):
    with pytest.raises(GoldenRulesError): verify_golden_rules(tmp_path)
def test_empty_goldenrules_fails_closed(tmp_path:Path):
    (tmp_path/'goldenrules.txt').write_text('   \n');
    with pytest.raises(GoldenRulesError): verify_golden_rules(tmp_path)
def test_valid_goldenrules_returns_hash(tmp_path:Path):
    (tmp_path/'goldenrules.txt').write_text('Daybagger rules'); status=verify_golden_rules(tmp_path); assert len(status.sha256)==64 and status.bytes_count>0
