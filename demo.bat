@echo off
rem Windows用: ダブルクリックでローカルデモを起動する
cd /d %~dp0
where py >nul 2>nul && (py demo.py) || (python demo.py)
pause
