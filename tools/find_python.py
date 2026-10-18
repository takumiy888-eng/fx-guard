"""start.bat から呼ばれる：使える Python（3.10以上）なら、その実行ファイルの場所を表示する"""
import sys

if sys.version_info[:2] >= (3, 10):
    print(sys.executable)
