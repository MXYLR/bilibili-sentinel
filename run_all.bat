@echo off
chcp 65001 >nul
title Bilibili Sentinel v2.7

set ROOT=%~dp0
set ROOT=%ROOT:~0,-1%
set VENV_PYTHON=%ROOT%\venv\Scripts\python.exe
set VENV_SCRAPY=%ROOT%\venv\Scripts\scrapy.exe
set LLM_CONFIG=%ROOT%\config\llm_config.json
set CLASH_PROXY_URL=socks5://192.168.1.104:7897
set CLASH_PROXY_ENABLED=1

:: Force unbuffered Python output (prevents log lag when stdout is redirected to file)
set PYTHONUNBUFFERED=1

echo.
echo ============================================================
echo   Bilibili Sentinel v2.7
echo ============================================================
echo.

:: [0] Check Environment
echo [0/5] Checking environment...
echo.

if exist "%VENV_PYTHON%" (
    echo   [OK] Python venv found  ^(%VENV_PYTHON%^)
) else (
    echo   [FAIL] Python venv not found!  Run: python -m venv venv
    pause
    exit /b 1
)

:: Check Redis db=1 (TCP port check with 2s timeout, no hang)
powershell -Command "$tcp=New-Object Net.Sockets.TcpClient;try{$r=$tcp.BeginConnect('127.0.0.1',6379,$null,$null);if($r.AsyncWaitHandle.WaitOne(2000)){$tcp.EndConnect($r);Write-Host 'REDIS_OK'}}catch{}finally{$tcp.Close()}" > "%TEMP%\redis_check.tmp" 2>nul
set REDIS_FOUND=0
findstr /c:"REDIS_OK" "%TEMP%\redis_check.tmp" >nul 2>&1 && set REDIS_FOUND=1
del "%TEMP%\redis_check.tmp" 2>nul
if %REDIS_FOUND%==1 (
    echo   [OK] Redis db=1 reachable
) else (
    echo   [WARN] Redis db=1 not reachable ^(crawler queues may fail^)
)

:: LLM config check (multi-provider: DeepSeek V4 / OpenAI / Custom)
set LLM_READY=0
set LLM_PROVIDER=unknown
if defined DEEPSEEK_API_KEY (
    set LLM_READY=1
    set LLM_PROVIDER=DeepSeek
)
if defined OPENAI_API_KEY (
    set LLM_READY=1
    set LLM_PROVIDER=OpenAI
)
if exist "%LLM_CONFIG%" (
    for /f "usebackq delims=" %%k in (`powershell -Command "try{$c=Get-Content '%LLM_CONFIG%' -Raw|ConvertFrom-Json;if($c.api_key){Write-Host $c.api_key}}catch{}" 2^>nul`) do set "LLM_CFG_KEY=%%k"
    for /f "usebackq delims=" %%p in (`powershell -Command "try{$c=Get-Content '%LLM_CONFIG%' -Raw|ConvertFrom-Json;if($c.provider){Write-Host $c.provider}}catch{}" 2^>nul`) do set "LLM_CFG_PROVIDER=%%p"
)
:: If llm_config.json has api_key but env vars are not set, use config file values
if defined LLM_CFG_KEY if %LLM_READY%==0 (
    if "%LLM_CFG_PROVIDER%"=="deepseek" set "DEEPSEEK_API_KEY=%LLM_CFG_KEY%" && set LLM_READY=1 && set LLM_PROVIDER=DeepSeek
    if "%LLM_CFG_PROVIDER%"=="openai"   set "OPENAI_API_KEY=%LLM_CFG_KEY%"   && set LLM_READY=1 && set LLM_PROVIDER=OpenAI
    if "%LLM_CFG_PROVIDER%"=="custom"   set "OPENAI_API_KEY=%LLM_CFG_KEY%"   && set LLM_READY=1 && set LLM_PROVIDER=自定义
)
:: Ensure DEEPSEEK_API_KEY / OPENAI_API_KEY are exported to child processes
if defined DEEPSEEK_API_KEY if %LLM_READY%==1 set LLM_PROVIDER=DeepSeek
if defined OPENAI_API_KEY   if %LLM_READY%==1 set LLM_PROVIDER=OpenAI

if %LLM_READY%==1 (
    echo   [OK] LLM ready ^(%LLM_PROVIDER%^)
) else (
    echo   [INFO] LLM disabled - configure provider + API Key in Settings page
)

:: AICU deep analysis check
set AICU_ENABLED=0
if exist "%LLM_CONFIG%" (
    for /f "usebackq delims=" %%d in (`powershell -Command "try{$c=Get-Content '%LLM_CONFIG%' -Raw|ConvertFrom-Json;if($c.deep_analysis_enabled){Write-Host '1'}}catch{}" 2^>nul`) do set "AICU_ENABLED=%%d"
)
if "%AICU_ENABLED%"=="1" (
    echo   [OK] AICU Deep Analysis enabled ^(对高风险账号自动获取历史评论/弹幕^)
) else (
    echo   [INFO] AICU Deep Analysis disabled ^(Settings 页面启用^)
)

:: Clash Verge proxy check (SOCKS5 — TCP port check with 2s timeout)
if %CLASH_PROXY_ENABLED%==1 (
    echo   [TEST] Checking Clash Verge proxy: %CLASH_PROXY_URL%
    powershell -Command "$u='%CLASH_PROXY_URL%' -replace '^socks5://','';$h,$p=$u -split ':',2;$p=[int]$p;try{$tcp=New-Object Net.Sockets.TcpClient;$r=$tcp.BeginConnect($h,$p,$null,$null);if($r.AsyncWaitHandle.WaitOne(2000)){$tcp.EndConnect($r);Write-Host 'CLASH_OK'}else{throw}}catch{}finally{$tcp.Close()}" > "%TEMP%\clash_check.tmp" 2>nul
    findstr /c:"CLASH_OK" "%TEMP%\clash_check.tmp" >nul 2>&1 && (echo   [OK] Clash proxy port reachable) || (echo   [WARN] Clash proxy port unreachable - B站 API requests may fail)
    del "%TEMP%\clash_check.tmp" 2>nul
) else (
    echo   [INFO] Clash proxy disabled (CLASH_PROXY_ENABLED=0)
)

:: Clean stale __pycache__ (skip venv entirely for speed)
powershell -Command "$dirs=Get-ChildItem '%ROOT%' -Directory -Exclude 'venv','.git','.idea','.vscode' -ErrorAction SilentlyContinue; $dirs | ForEach-Object { Get-ChildItem $_.FullName -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
:: Clean stale logs from previous runs (keeps logs fresh per session)
if exist "%ROOT%\data\logs" (
    del /q "%ROOT%\data\logs\*.log" 2>nul
    del /q "%ROOT%\data\logs\*.json" 2>nul
)
:: Create directories (recreate logs if it was removed)
if not exist "%ROOT%\data\logs"     mkdir "%ROOT%\data\logs"
if not exist "%ROOT%\data\videos"   mkdir "%ROOT%\data\videos"
if not exist "%ROOT%\data\comments" mkdir "%ROOT%\data\comments"
if not exist "%ROOT%\data\users"    mkdir "%ROOT%\data\users"
if not exist "%ROOT%\data\danmaku"  mkdir "%ROOT%\data\danmaku"
if not exist "%ROOT%\data\reports"  mkdir "%ROOT%\data\reports"

:: Module checks (quick existence only, no WMI scanning)
if exist "%ROOT%\config\base_config.py"               (echo   [OK] Config)      else (echo   [WARN] config/ missing)
if exist "%ROOT%\bilibili_crawler\login\bilibili_login.py" (echo   [OK] Login)  else (echo   [WARN] login/ missing)
if exist "%ROOT%\analyzer\llm_analyzer.py" (
    if %LLM_READY%==1 (echo   [OK] LLM Analyzer + API Key ^(%LLM_PROVIDER%^)) else (echo   [OK] LLM Analyzer ^(no API Key^))
) else (echo   [INFO] LLM Analyzer not found)

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

echo.
echo [1/4] Starting spiders (video + comment)...
:: Note: comment spider auto-discovers BVs; user spider gets MID seeds from comment spider
:: User spider + UP主 seed linkage via Dashboard: Crawler 页面
start "Bilibili Video Spider"   /MIN cmd /c %VENV_SCRAPY% crawl bilibili_video   ^> %ROOT%\data\logs\video.log   2^>^&1
start "Bilibili Comment Spider" /MIN cmd /c %VENV_SCRAPY% crawl bilibili_comment ^> %ROOT%\data\logs\comment.log 2^>^&1
echo   [OK] Video + Comment spiders launched
echo   [TIP] User spider via Dashboard: Crawler 页面 ^(注入UID自动联动视频+评论爬虫^)

echo.
echo [2/4] Starting Dashboard on port 5001...
start "Bilibili Sentinel Dashboard" /MIN cmd /c %VENV_PYTHON% -u dashboard\app.py ^> %ROOT%\data\logs\dashboard.log 2^>^&1
echo   [OK] Dashboard starting...

:: Wait a moment for services to initialize
%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul

echo.
echo [3/4] Checking service health...
powershell -Command "try{$r=Invoke-WebRequest -Uri http://localhost:5001/api/system/health -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop;Write-Host '  [OK] Dashboard healthy'}catch{Write-Host '  [WARN] Dashboard not responding yet'}"

echo.
echo [4/4] Opening browser...
start http://localhost:5001

echo.
echo ============================================================
echo   All services started!
echo ============================================================
echo.
echo   Dashboard pages:
echo     Home:     http://localhost:5001\
echo     Crawler:  http://localhost:5001\crawler   ^(3 spiders: video/comment/user^)
echo     Video:    http://localhost:5001\video\[bvid]
echo     Settings: http://localhost:5001\settings   ^(LLM多Provider + AICU深度分析 + 代理^)
echo     WaterArmy: http://localhost:5001\water-army  ^(水军账号管理^)
echo.
echo   Logs:  %ROOT%\data\logs\
echo   LLM:   %LLM_CONFIG%
if %LLM_READY%==1 (
    echo   LLM:   Enabled ^(%LLM_PROVIDER%^)
) else (
    echo   LLM:   Disabled
)
if %CLASH_PROXY_ENABLED%==1 (
    echo   Proxy: %CLASH_PROXY_URL% ^(Clash Verge^)
) else (
    echo   Proxy: Disabled
)
if "%AICU_ENABLED%"=="1" (
    echo   AICU:  Deep Analysis Enabled ^(高风险账号历史数据回溯^)
)
echo.
echo   v2.7: LLM初筛Modal化 + UP主种子联动 + AICU弹幕集成
echo   3爬虫: video/comment/user, 注入UID自动联动视频+评论
echo   分析流程: Scorer -> LLM初筛 -> AICU深度分析 -> 报告
echo.
echo ============================================================
echo.
echo   Press any key to STOP all services...
pause >nul

echo.
echo ============================================================
echo   Shutting down...
echo ============================================================
echo.

:: Stage 1: Graceful shutdown via Dashboard API (preferred)
echo [1/3] Requesting graceful shutdown via API...
powershell -Command "try { Invoke-WebRequest -Uri http://localhost:5001/api/system/shutdown -Method POST -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop | Out-Null; Write-Host '  [OK] Graceful shutdown completed' } catch { Write-Host '  [INFO] API shutdown not available (Dashboard may already be down)' }"
:: Give API time to stop spiders and close
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul

:: Stage 2: Clean up remaining processes (lightweight fallback)
echo [2/3] Cleaning up remaining processes...
:: Kill by window title (no PID scanning, no WMI, no hidden windows)
taskkill /FI "WINDOWTITLE eq Bilibili Video Spider*"    /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Bilibili Comment Spider*"  /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Bilibili User Spider*"     /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Bilibili Sentinel Dashboard*" /F >nul 2>&1
echo   [OK] Processes cleaned

:: Stage 3: Clean up generated files
echo [3/3] Cleaning up temp files...
if exist "%ROOT%\data\spider_pids.txt"    del "%ROOT%\data\spider_pids.txt" >nul 2>&1
if exist "%ROOT%\data\spider_state.json"  del "%ROOT%\data\spider_state.json" >nul 2>&1
if exist "%ROOT%\data\logs\*.log"         del "%ROOT%\data\logs\*.log" >nul 2>&1
echo   [OK] Cleanup complete

echo.
echo   All services stopped.
echo ============================================================
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
exit /b 0
