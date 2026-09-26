@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" http://127.0.0.1:5000
python serve.py 5000
pause
