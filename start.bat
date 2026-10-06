@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
title BrainStorm

cd /d "%~dp0"

echo.
echo  +----------------------------------+
echo  ^|   BrainStorm  -  Zapusk          ^|
echo  +----------------------------------+
echo.

:: ==================================================
:: 1. Python
:: ==================================================
echo  [1/5] Proverka Python...
python --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo  OSHIBKA: Python ne nayden!
    echo  Skaychayte: https://python.org/downloads
    echo  Pri ustanovke vyberte "Add to PATH"
    echo.
    pause
    exit /b 1
)
python _check.py pyver > "%TEMP%\bs_pyver.txt" 2>&1
set /p PYVER=<"%TEMP%\bs_pyver.txt"
del "%TEMP%\bs_pyver.txt" > nul 2>&1
if "!PYVER:~0,3!"=="OLD" (
    echo  OSHIBKA: Nuzhen Python 3.10+
    pause
    exit /b 1
)
echo  OK: Python !PYVER:OK:=!

:: ==================================================
:: 2. .env
:: ==================================================
echo.
echo  [2/5] Nastroyka .env...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" > nul
        echo  Sozdan .env iz .env.example
        echo.
        echo  +--------------------------------------------------+
        echo  ^|  Otkroyte .env i vstavyte GIGACHAT_CREDENTIALS   ^|
        echo  +--------------------------------------------------+
        echo.
        set /p "DOEDIT=  Otkryt .env v Notepad? [Y/N]: "
        if /i "!DOEDIT!"=="Y" (
            notepad .env
            timeout /t 2 /nobreak > nul
        )
    ) else (
        echo  .env.example ne nayden, sozdayu bazovy .env
        (
            echo SECRET_KEY=brainstorm-change-me
            echo HOST=0.0.0.0
            echo PORT=5000
            echo DEBUG=false
            echo LOG_LEVEL=INFO
        ) > .env
    )
) else (
    echo  OK: .env naydyen
)

:: ==================================================
:: 3. pip install
:: ==================================================
echo.
echo  [3/5] Ustanovka zavisimostey...
if not exist "requirements.txt" (
    echo  OSHIBKA: requirements.txt ne nayden!
    pause
    exit /b 1
)
python -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo  OSHIBKA pri ustanovke zavisimostey!
    echo  Poprobuy vruchnuyu: pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo  OK: Zavisimosti ustanovleny

:: ==================================================
:: 4. GigaChat check
:: ==================================================
echo.
echo  [4/5] Proverka GigaChat...

set "GC=none"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if "%%A"=="GIGACHAT_CREDENTIALS" set "GC=%%B"
)

set "AI_LINE=Fallback-bank (klyuch ne zadyan)"

if "!GC!"=="none" (
    echo  INFO: Klyuch ne nayden v .env
) else (
    python _check.py gigachat "!GC!" > "%TEMP%\bs_gc.txt" 2>&1
    set /p GC_RESULT=<"%TEMP%\bs_gc.txt"
    del "%TEMP%\bs_gc.txt" > nul 2>&1
    if "!GC_RESULT!"=="OK" (
        echo  OK: GigaChat podklyuchen!
        set "AI_LINE=GigaChat (Sberbank) - podklyuchen OK"
    ) else (
        echo  WARN: GigaChat nedostupen, budet fallback.
        set "AI_LINE=Fallback-bank (oshibka podklyucheniya)"
    )
)

:: ==================================================
:: 5. Port + IP + Launch
:: ==================================================
echo.
echo  [5/5] Opredelyayu adres servera...

set "PORT=5000"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if "%%A"=="PORT" set "PORT=%%B"
)

python _check.py ip > "%TEMP%\bs_ip.txt" 2>&1
set /p LOCAL_IP=<"%TEMP%\bs_ip.txt"
del "%TEMP%\bs_ip.txt" > nul 2>&1
if "!LOCAL_IP!"=="" set "LOCAL_IP=127.0.0.1"

echo.
echo  +--------------------------------------------------+
echo  ^|    BrainStorm  --  server zapushchen!            ^|
echo  +--------------------------------------------------+
echo  ^|    Lokalno:  http://localhost:!PORT!                 ^|
echo  ^|    Po seti:  http://!LOCAL_IP!:!PORT!             ^|
echo  +--------------------------------------------------+
echo  ^|    AI: !AI_LINE!
echo  +--------------------------------------------------+
echo  ^|    Ctrl+C -- ostanovit server                    ^|
echo  +--------------------------------------------------+
echo.

python run.py

echo.
echo  +--------------------------------------------------+
echo  ^|  Server ostanovlen.                              ^|
echo  +--------------------------------------------------+
echo.
pause
endlocal
