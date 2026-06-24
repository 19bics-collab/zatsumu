# zatsumu 操作ウィンドウ版クライアントを単体 .exe にビルドする (Windows / PowerShell)
# (大きな着席/退席ボタン・状態表示・作業区分切替のある操作画面を出す)
#
#   使い方:  PowerShell で  .\build_client.ps1
#
# 完成物:  dist\zatsumu.exe  (Python のインストール不要で配布できる)
# 配布時は exe と同じフォルダに zatsumu_config.json を置き、サーバ URL を記載:
#   { "server": "https://kintai.example.com" }
# 各メンバーは初回起動で自分のトークンを一度だけ入力すれば、以降はダブルクリックで着席。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== 依存ライブラリと PyInstaller を準備 ==" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r client/requirements.txt pyinstaller

Write-Host "== ビルド ==" -ForegroundColor Cyan
# --windowed: コンソール窓を出さない / --onefile: 単一 exe
# pystray は OS バックエンドを動的 import するため submodule をまとめて収集する
python -m PyInstaller --noconfirm --clean --onefile --windowed --name 勤怠管理 `
  --collect-submodules pystray `
  --hidden-import PIL._tkinter_finder `
  run_client.py

# 配布フォルダに設定テンプレートを置く (既存の設定は上書きしない)
$cfg = "dist\zatsumu_config.json"
if (-not (Test-Path $cfg)) {
  Set-Content -Path $cfg -Value '{ "server": "https://kintai.example.com" }' -Encoding UTF8
}

Write-Host ""
Write-Host "完成: dist\勤怠管理.exe" -ForegroundColor Green
Write-Host "接続先(既定 https://kintai.yadotsugi.jp)は client/config.py の DEFAULT_SERVER に埋め込み済み。" -ForegroundColor Green
Write-Host "従業員には dist\勤怠管理.exe を1つ渡すだけ(初回にトークン入力)。別サーバにする場合のみ zatsumu_config.json を同梱。" -ForegroundColor Green
