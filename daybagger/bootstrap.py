from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from daybagger.errors import GoldenRulesError

@dataclass(frozen=True, slots=True)
class GoldenRulesStatus:
    path: Path; sha256: str; bytes_count: int

def verify_golden_rules(repo_root: Path) -> GoldenRulesStatus:
    path = repo_root.resolve() / 'goldenrules.txt'
    if not path.exists() or not path.is_file():
        raise GoldenRulesError(f"Missing mandatory goldenrules.txt at repository root: {repo_root.resolve()}")
    content = path.read_bytes()
    if not content.strip(): raise GoldenRulesError('goldenrules.txt exists but is empty.')
    return GoldenRulesStatus(path, sha256(content).hexdigest(), len(content))
