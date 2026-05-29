@echo off
chcp 65001 >nul
title Bilibili Sentinel v2.0

set ROOT=%~dp0
set ROOT=%ROOT:~0,-1%
set VENV_PYTHON=%ROOT%\..\distributed-crawler\venv\Scripts\python.exe
set VENV_SCRAPY=%ROOT%\..\distributed-crawler\venv\Scripts\scrapy.exe

echo.
echo ============
echo   Bilibili Sentinel v2.0
echo ============
echo.

:: [0] Check Environment
echo [0/5] Checking environment...
echo.

:: Python venv
if exist "%VENV_PYTHON%" (
    echo   [OK] Python venv found
) else (
    echo   [FAIL] Python venv not found!
    echo   Expected: %VENV_PYTHON%
    pause
    exit /b 1
)

:: Redis
"C:\Program Files\Redis\redis-cli.exe" ping >nul 2>&1 && (
    echo   [OK] Redis is running
) || (
    echo   [WARN] Redis not reachable
)

:: Project modules
if exist "%ROOT%\config\base_config.py" (echo   [OK] Config package) else (echo   [WARN] config/ missing)
if exist "%ROOT%\proxy\proxy_ip_pool.py" (echo   [OK] Proxy module) else (echo   [WARN] proxy/ missing)
if exist "%ROOT%\store\store_factory.py" (echo   [OK] Store module) else (echo   [WARN] store/ missing)
if exist "%ROOT%\cache\local_cache.py" (echo   [OK] Cache module) else (echo   [WARN] cache/ missing)
if exist "%ROOT%\tools\stealth_utils.py" (echo   [OK] Anti-detection) else (echo   [WARN] tools/ missing)
if exist "%ROOT%\bilibili_crawler\login\bilibili_login.py" (echo   [OK] Login module) else (echo   [WARN] login/ missing)

:: Ensure log dir exists
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

echo.
:: [1] Seed Injection
echo [1/5] Injecting hot video seeds...
cd /d "%ROOT%"
"%VENV_PYTHON%" deploy\deploy_bilibili.py --hot --pages 3
if errorlevel 1 (
    echo   [WARN] Seed injection failed, continuing...
) else (
    echo   [OK] Seeds injected
)

echo.
:: [2][3] Start Spiders (completely hidden via powershell)
echo [2/5] Starting Video Spider (hidden)...
powershell -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%VENV_SCRAPY%' -ArgumentList 'crawl','bilibili_video' -WorkingDirectory '%ROOT%' -RedirectStandardOutput '%ROOT%\data\logs\video.log' -RedirectStandardError '%ROOT%\data\logs\video.err.log'"

echo [3/5] Starting Comment Spider (hidden)...
powershell -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%VENV_SCRAPY%' -ArgumentList 'crawl','bilibili_comment' -WorkingDirectory '%ROOT%' -RedirectStandardOutput '%ROOT%\data\logs\comment.log' -RedirectStandardError '%ROOT%\data\logs\comment.err.log'"

echo   [OK] Both spiders launched (logs: data\logs\)

echo.
:: [4] Start Dashboard (hidden)
echo [4/5] Starting Dashboard on port 5001 (hidden)...
powershell -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%VENV_PYTHON%' -ArgumentList 'dashboard\app.py' -WorkingDirectory '%ROOT%' -RedirectStandardOutput '%ROOT%\data\logs\dashboard.log' -RedirectStandardError '%ROOT%\data\logs\dashboard.err.log'"
echo   [OK] Dashboard starting...

echo.
:: [5] Open Browser
echo [5/5] Opening browser...
%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
start http://localhost:5001

echo.
echo ============================================================
echo   All services started!
echo ============================================================
echo.
echo   Dashboard pages:
echo     Home:     http://localhost:5001/
echo     Crawler:  http://localhost:5001/crawler
echo     Video:    http://localhost:5001/video/[bvid]
echo     Settings: http://localhost:5001/settings
echo.
echo   Logs: data\logs\
echo.
echo ============================================================
echo.
echo   Press any key to STOP all services...
pause >nul

:: === Graceful Shutdown ===
echo.
echo ============================================================
echo   Shutting down...
echo ============================================================
echo.

echo [1/3] Stopping Dashboard on port 5001...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5001.*LISTENING" 2^>nul') do taskkill /PID %%a /F >nul 2>&1

echo [2/3] Stopping Scrapy spiders...
taskkill /FI "IMAGENAME eq scrapy.exe" /F >nul 2>&1

echo [3/3] Cleaning up Python background processes...
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *bilibili*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq powershell.exe" /F >nul 2>&1

echo.
echo   All services stopped.
echo ============================================================
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
exit /b 0
