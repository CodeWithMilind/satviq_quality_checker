@echo off
REM ============================================================
REM  SATVIQ - Resume Intelligence : Windows startup script
REM  Starts the FastAPI backend, which also serves the frontend.
REM ============================================================ 

cd /d "%~dp0"

REM Activate an existing virtual environment if one is present.
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else (
    echo No virtual environment found (.venv or venv).
    echo Create one and install dependencies first:
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo Continuing with the system Python...
)

echo.
echo SATVIQ - Resume Intelligence is starting...
echo Open this URL in your browser:  http://127.0.0.1:8000
echo Press Ctrl+C to stop the server.
echo.

python -m uvicorn api.main:app --host 127.0.0.1 --port 8000

pause
