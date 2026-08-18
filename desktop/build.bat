@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM   录音笔控制台 — PyInstaller 一键打包脚本（Windows）
REM   用法：在 PowerShell 中双击或执行： .\build.bat
REM ============================================================

echo.
echo [1/5] 检查工作目录 ...
cd /d "%~dp0"
set "PROJECT_ROOT=%cd%"
cd record
echo       项目根：%PROJECT_ROOT%

REM ============================================================
echo.
echo [2/5] 创建/激活干净的 venv ...
REM  —— 必须用干净 venv 打包！别用 conda、别用全局环境。
REM  —— 否则会把系统里装的 pandas/torch/PyQt 等几百个没用的包都收进去，
REM     exe 体积爆炸 + 运行缺模块概率飙升 90%。
REM ============================================================
if not exist .venv (
    echo       .venv 不存在，正在创建...
    py -3 -m venv .venv || goto :python_missing
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto :venv_fail

REM ============================================================
echo.
echo [3/5] 安装必需依赖 ...
REM 注意：不要 --upgrade 到最新版，可能与 funasr-onnx 不兼容。
REM ============================================================
python -m pip install --upgrade pip 2>nul

echo       安装 bleak 录音笔蓝牙依赖...
pip install -r requirements.txt 1>nul 2>>build_pip.log
if errorlevel 1 goto :pip_fail

echo       安装 ASR 推理引擎（funasr-onnx + onnxruntime）...
pip install -r requirements-asr.txt 1>nul 2>>build_pip.log
if errorlevel 1 goto :pip_fail

echo       安装 FastAPI Web 框架...
pip install -r requirements-web.txt 1>nul 2>>build_pip.log
if errorlevel 1 goto :pip_fail

echo       安装音频处理 pydub ...
pip install pydub soundfile 1>nul 2>>build_pip.log
if errorlevel 1 goto :pip_fail

echo       安装打包工具 PyInstaller ...
pip install pyinstaller 1>nul 2>>build_pip.log
if errorlevel 1 goto :pip_fail

REM ============================================================
echo.
echo [4/5] 清理旧产物 ...
REM 每次打包都清 dist/build 目录，避免旧文件残留
REM ============================================================
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
if exist "录音笔控制台.spec" del /q "录音笔控制台.spec"

REM ============================================================
echo.
echo [5/5] 执行 PyInstaller 打包（约 1-3 分钟，首次更长）...
REM   用 spec 打包（spec 里声明了 datas + hiddenimports + excludes）
REM ============================================================
set "PROJECT_ROOT=%PROJECT_ROOT%"
pyinstaller build.spec --noconfirm --clean
if errorlevel 1 goto :pyi_fail

echo.
echo ============================================================
echo   打包完成！
echo ============================================================
echo   产物目录：dist\录音笔控制台\
echo.
echo   其中：
echo     录音笔控制台.exe    —— 双击即运行
echo     _internal\web\     —— 前端静态资源
echo     _internal\ffmpeg\  —— ffmpeg/ffprobe 二进制
echo.
echo   运行逻辑：
echo     双击录音笔控制台.exe → 启动本地 Web 服务 → 自动打开浏览器
echo     首次转写会自动下载 ASR 模型（SenseVoiceSmall，约 242MB，
echo     需要联网一次；之后可离线使用）
echo.
echo   发给别人用的方法：
echo     把 dist\录音笔控制台\ 整个文件夹压缩成 zip，发给对方，
echo     解压后双击录音笔控制台.exe 即可。
echo     —— 对方不需要装 Python、ffmpeg、任何依赖。
echo.
REM  打开 dist 目录给用户看
start explorer "dist"
goto :eof


REM ============ 错误处理 ============

:python_missing
echo.
echo [错误] 找不到 python 3 解释器。
echo        请先从 https://www.python.org/downloads/ 安装 Python 3.8 或更高版本。
echo        安装时务必勾选 "Add Python to PATH"。
pause
exit /b 1

:venv_fail
echo.
echo [错误] 创建/激活 venv 失败。
echo        PowerShell 可能禁止脚本执行，请先运行：
echo            Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
echo        或直接用 CMD（不是 PowerShell）执行本脚本。
pause
exit /b 1

:pip_fail
echo.
echo [错误] pip 安装依赖失败。详情见：build_pip.log
echo        1. 检查是否能联网访问 https://pypi.org
echo        2. 国内建议加镜像：
echo             pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
echo        3. 删掉 .venv 目录后重跑脚本（重建干净环境）
pause
exit /b 1

:pyi_fail
echo.
echo [错误] PyInstaller 打包失败。
echo        常见原因与解决：
echo        1) 如果报"No module named xxx" —— 打开 build.spec，
echo           把缺的模块名加到 hiddenimports = [...] 里。
echo        2) 如果报找不到 ffmpeg —— 确认 PROJECT_ROOT 下存在
echo           ffmpeg-8.1.2-essentials_build/bin/ffmpeg.exe
echo        3) 如果打出来 exe 体积超大（2GB+）— 确认用的是
echo           venv 而不是 conda/全局环境，重新按步骤 2/3 建干净环境。
pause
exit /b 1
