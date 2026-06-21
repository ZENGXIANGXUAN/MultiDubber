@echo off
title Auto Dubbing Studio
:: 切换到 .bat 文件所在目录
cd /d "%~dp0"

:: 使用 python.exe 启动（保留控制台以便查看错误信息）
:: 如果用 pythonw.exe 则任何启动崩溃都看不到
".venv\Scripts\python.exe" "gui.pyw"

pause