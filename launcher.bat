@echo off
:: Pocket-Claude Bot launcher with auto-restart
:: Place shortcut in shell:startup for auto-run at login

cd /d "C:\Users\H\Desktop\deepseek-feishu-bot"

:loop
echo [%date% %time%] Starting bot...
python bot.py
echo [%date% %time%] Bot exited (code=%ERRORLEVEL%), restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
