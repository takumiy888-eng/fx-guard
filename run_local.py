"""PCで試す用：Botと管理画面を1つのプロセスで起動（http://127.0.0.1:8080）"""
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.config import Store  # noqa: E402
from core.engine import Engine  # noqa: E402

BASE = Path(__file__).resolve().parent
if __name__ == "__main__":
    store = Store(BASE)
    if not store.has_password():
        store.set_password("fxguard-local")
        print("ローカル用パスワード: fxguard-local（設定タブで変更できます）")
    threading.Thread(target=Engine(BASE).run_forever, daemon=True).start()
    from web.app import app
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8080")).start()
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
