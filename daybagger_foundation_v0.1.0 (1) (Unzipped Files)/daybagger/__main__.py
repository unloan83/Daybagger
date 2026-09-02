from __future__ import annotations
import argparse
from pathlib import Path
from daybagger.app import run_foundation_boot

def main() -> int:
    parser = argparse.ArgumentParser(description="Boot the Daybagger foundation.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    return run_foundation_boot(repo_root=args.repo_root, config_path=args.config)

if __name__ == "__main__":
    raise SystemExit(main())
