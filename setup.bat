@echo off
echo ============================================
echo   GENESIS AI Orchestration System Setup
echo ============================================
echo.

echo [1/3] Installing Python dependencies...
pip install -e . --quiet
if errorlevel 1 (
    echo ERROR: pip install failed. Make sure Python and pip are in PATH.
    pause
    exit /b 1
)
echo       Done.
echo.

echo [2/3] Initializing config at %%USERPROFILE%%\.genesis\config.toml
python -m genesis init
echo.

echo [3/3] Setup complete!
echo.
echo   To start Genesis, run:
echo     genesis
echo.
echo   Or directly:
echo     python -m genesis
echo.
echo   First time? Edit your config:
echo     genesis config edit
echo.
pause
