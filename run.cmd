@echo off
rem qq-bridge-memory sidecar launcher (v2.3)
rem - binds 127.0.0.1 only
rem - single worker: SQLite does not tolerate multiple writers
rem - QQ_MEMORY_TOKEN is optional; without it /v1/* only accepts localhost callers
setlocal
cd /d "%~dp0"

if not defined QQ_MEMORY_PORT set "QQ_MEMORY_PORT=8787"
rem optional: where the 200MB model cache lives (default: .\models)
rem set "QQ_MEMORY_MODEL_DIR=D:\cache\qq-memory-models"
if not defined QQ_MEMORY_TOKEN echo [WARN] QQ_MEMORY_TOKEN is not set: /v1/* will only accept localhost

".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port %QQ_MEMORY_PORT% --workers 1
if errorlevel 1 (
  echo.
  echo [ERROR] 侧车退出，退出码 %errorlevel%
  echo 常见原因：端口被占用、数据库打不开、依赖缺失
  echo 完整报错见上面的输出，也可以看 logs 目录
  pause
)
