@echo off
chcp 65001 >nul
REM 개발용: 설치한 Python으로 바로 실행
cd /d "%~dp0"
py -3 -m receipt_renamer %*
