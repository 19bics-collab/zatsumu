"""PyInstaller 用エントリポイント — トレイ常駐クライアントを起動する.

.exe にパッケージするのはこのファイル。相対 import を含む client パッケージを
正しく取り込むため、パッケージ外の通常スクリプトとして main を呼び出す。
ビルド: build_client.ps1 を参照。
"""
from client.tray import main

if __name__ == "__main__":
    main()
