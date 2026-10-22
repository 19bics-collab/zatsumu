"""m2m清掃管理 → ヤドツギ の読み取り係.

m2m の管理画面 (manager-cleaning.m2msystems.cloud) をヘッドレスブラウザで開いて清掃一覧を読み、
ヤドツギの内部API (POST /api/internal/m2m/cleanings) に送る。DB への反映ルールはヤドツギ側が持つ。

zatsumu 本体 (server/) とは独立したコンテナで動かす (ブラウザが固まってもメール・勤怠を巻き込まない)。
"""
