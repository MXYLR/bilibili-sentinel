@echo off
set PYTHONUNBUFFERED=1
set ROOT=D:\腾讯云黑客松\bilibili-sentinel
set PY=%ROOT%\venv\Scripts\python.exe
set SCRAPY=%ROOT%\venv\Scripts\scrapy.exe

echo Starting Video Spider...
start "Bilibili Video Spider" /MIN %SCRAPY% crawl bilibili_video > %ROOT%\data\logs\video.log 2>&1

echo Starting Comment Spider...
start "Bilibili Comment Spider" /MIN %SCRAPY% crawl bilibili_comment > %ROOT%\data\logs\comment.log 2>&1

echo Starting Dashboard...
start "Bilibili Dashboard" /MIN %PY% -u %ROOT%\dashboard\app.py > %ROOT%\data\logs\dashboard.log 2>&1

echo All services launched.
