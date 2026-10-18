"""管理画面プロセス（127.0.0.1:8080。外部からは Caddy が HTTPS で中継）"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("FXGUARD_PORT", "8080"))
    host = os.environ.get("FXGUARD_BIND", "127.0.0.1")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=4)
    except ImportError:  # ローカル確認用
        app.run(host=host, port=port, debug=False, threaded=True)
