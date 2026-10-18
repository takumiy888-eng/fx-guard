"""PCで動かす：Botと管理画面を1つのプロセスで起動（start.bat から呼ばれる）

- このPCのブラウザ: http://127.0.0.1:8080 （パスワード不要）
- 同じWi-Fiのスマホ: http://<このPCのIP>:8080 （パスワードあり。設定タブに表示）
- 動いている間はPCのスリープを防ぐ（終了すると元に戻る）
- すでに起動中なら、新しく起動せずに画面だけ開く
"""
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.environ["FXGUARD_LOCAL"] = "1"
os.environ.setdefault("FXGUARD_HOME", str(BASE))
sys.path.insert(0, str(BASE))

PORT = int(os.environ.get("FXGUARD_PORT", "8080"))
URL = f"http://127.0.0.1:{PORT}"


def already_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def lan_ip() -> str:
    """このPCの家庭内ネットワークでのIP（実際には通信しない）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def prevent_sleep():
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    except Exception:  # noqa: BLE001  Windows以外では何もしない
        pass


def main():
    open_browser = "--no-browser" not in sys.argv
    if already_running():
        print("FX Guard はすでに起動しています。画面を開きます。")
        if open_browser:
            webbrowser.open(URL)
        return

    from core.config import Store
    from core.engine import Engine

    store = Store(BASE)
    sec = store._sec()
    if not sec.get("phone_password"):
        pw = secrets.token_urlsafe(6)
        store.set_password(pw)
        sec = store._sec()
        sec["phone_password"] = pw
        store._sec_save(sec)
    ip = lan_ip()
    os.environ["FXGUARD_LAN_URL"] = f"http://{ip}:{PORT}"

    prevent_sleep()
    threading.Thread(target=Engine(BASE).run_forever, daemon=True, name="engine").start()

    from web.app import app
    print("\n" + "=" * 60)
    print("  FX Guard が動いています（このウィンドウを閉じると止まります）")
    print(f"  このPCで見る : {URL}")
    print(f"  スマホで見る : http://{ip}:{PORT}  （同じWi-Fi・パスワードは設定タブ）")
    print("  初回は過去データの取得と学習に30分ほどかかります")
    print("=" * 60 + "\n")
    if open_browser:
        threading.Timer(2.0, lambda: webbrowser.open(URL)).start()
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=6)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
