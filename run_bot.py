"""Bot常駐プロセス（VPSでは systemd が起動・自動再起動する）"""
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("FXGUARD_HOME", Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.engine import Engine  # noqa: E402

if __name__ == "__main__":
    Engine(BASE).run_forever()
