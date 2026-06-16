"""PyInstaller 用エントリポイント — 操作ウィンドウ版クライアントを起動する.

.exe にパッケージするのはこのファイル。相対 import を含む client パッケージを
正しく取り込むため、パッケージ外の通常スクリプトとして main を呼び出す。
大きな着席/退席ボタンのある操作画面を出す (client.window)。
ビルド: build_client.ps1 を参照。
"""
from client.window import main

if __name__ == "__main__":
    main()
