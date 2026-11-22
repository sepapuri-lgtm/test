@echo off
chcp 65001 >nul
REM 영수증 파일명 변환기 - 단일 실행 파일(dist\ReceiptRenamer.exe) 만들기
cd /d "%~dp0"
py -3 -m pip install --upgrade pip || goto :error
py -3 -m pip install -r requirements.txt pyinstaller || goto :error
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name ReceiptRenamer ^
  --collect-all tkinterdnd2 --collect-all winrt --collect-all pillow_heif --collect-submodules receipt_renamer ^
  run_app.py || goto :error
echo.
echo 완료: dist\ReceiptRenamer.exe
echo 점검: dist\ReceiptRenamer.exe --selftest
pause
exit /b 0
:error
echo 빌드 실패
pause
exit /b 1
