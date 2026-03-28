@echo off
:: 1. 强制切换到当前 .bat 文件所在的目录，确保系统能准确找到 gui.pyw
cd /d "%~dp0"

:: 2. start 后面先加一个空引号 "" 作为标题，防止路径被误认为标题
:: 3. 建议使用 pythonw.exe 来运行 .pyw 文件，避免多余的黑框
start "" "D:\Users\xuan\Mycode\MultTTS\.venv\Scripts\pythonw.exe" "gui.pyw"

exit