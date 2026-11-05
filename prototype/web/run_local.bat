@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ================================================================
echo   门店补货与库存协同智能体 . 本地启动
echo  ================================================================
echo.

rem ---------- 0. 端口占用检测（重复双击时直接打开页面而不是报错）----------
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [提示] 端口 8765 已被占用，服务很可能已在运行。
    echo        即将为你打开 http://127.0.0.1:8765
    echo        如需重启服务，请先关闭旧的运行窗口再双击本脚本。
    echo.
    start "" http://127.0.0.1:8765
    timeout /t 5 >nul
    exit /b 0
)

rem ---------- 1. 定位 Python 解释器 ----------
rem 优先用系统 PATH 里的 python；找不到就探测 WorkBuddy 内置便携版。
set "PY=python"
python --version >nul 2>&1
if errorlevel 1 (
    set "PY="
    for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\3.*") do (
        if exist "%%d\python.exe" set "PY=%%d\python.exe"
    )
    if not defined PY (
        echo [ERR] 未找到 Python。
        echo       方式一：安装 Python 3.11+（勾选 Add Python to PATH）
        echo       方式二：确认 WorkBuddy 内置 Python 目录存在：
        echo       %USERPROFILE%\.workbuddy\binaries\python\versions\
        echo.
        pause
        exit /b 1
    )
)
for /f "tokens=2" %%v in ('"%PY%" --version 2^>^&1') do set PYVER=%%v
echo [OK]   Python %PYVER%
echo        解释器：%PY%

rem ---------- 2. 检查 .env ----------
if not exist ".env" (
    echo [WARN] 未找到 .env，正在从 .env.example 复制模板...
    copy ".env.example" ".env" >nul
    echo        请在弹出的记事本里填写 LLM_API_KEY，保存后重新双击本脚本。
    notepad ".env"
    pause
    exit /b 1
)
echo [OK]   已找到 .env

rem ---------- 3. 依赖检查（缺哪个装哪个，不重复装）----------
"%PY%" -c "import fastapi, uvicorn, pydantic, jwt, multipart" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Web 依赖不完整，开始安装 requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERR] 依赖安装失败，请检查网络或改用国内镜像：
        echo       "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)
"%PY%" -c "import faiss, jieba, rank_bm25, numpy, requests, pypdf" >nul 2>&1
if errorlevel 1 (
    echo [WARN] 检索依赖不完整，开始安装 requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERR] 依赖安装失败，请检查网络或改用国内镜像。
        pause
        exit /b 1
    )
)
echo [OK]   依赖齐全

rem ---------- 4. 配置自检（把错误挡在第一个请求之前）----------
"%PY%" config.py
if errorlevel 1 (
    echo [ERR] 配置存在错误，服务无法提供问答。
    echo       请修改 .env 后重新运行；配置模板见 .env.example
    echo.
    pause
    exit /b 1
)

rem ---------- 5. 启动服务 ----------
echo  正在启动服务...
echo  首次启动或切换向量模型时会重建索引，约需 10-40 秒，请勿关闭本窗口。
echo  访问地址：http://127.0.0.1:8765
echo  默认账号：admin / admin@123      店长：store01 / store@123
echo  停止服务：在本窗口按 Ctrl+C
echo.
start "" /min cmd /c "timeout /t 6 >nul & start http://127.0.0.1:8765"
"%PY%" -m uvicorn server:app --host 127.0.0.1 --port 8765

echo.
echo 服务已停止。
pause
