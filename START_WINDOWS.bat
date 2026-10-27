@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo           SETKA AFTER DARK
 echo ================================================
echo Site: http://127.0.0.1:8080
echo Admin: http://127.0.0.1:8080/admin
echo Private admin code: data\admin_code.txt
echo.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 server.py
) else (
    python server.py
)
echo.
echo Press any key to close
pause >nul
