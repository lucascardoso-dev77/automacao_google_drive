@echo off
title Dashboard - Automacao Documental

:: ============================================================
::  Sobe o dashboard de monitoramento (porta 5000) e mantem ativo.
::  Roda em processo separado da automacao para nao cair junto
::  com o reinicio do loop do main.py.
:: ============================================================
set PYTHON=py -3.12
set SCRIPT=dashboard_api.py

echo.
echo  =============================================
echo   Dashboard de Monitoramento
echo   http://localhost:5000
echo  =============================================
echo.

%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Verifique se esta instalado e no PATH.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERRO] Arquivo "%SCRIPT%" nao encontrado.
    echo        Execute este .bat na mesma pasta do projeto.
    pause
    exit /b 1
)

:DASHLOOP
%PYTHON% %SCRIPT%

echo.
echo [%date% %time%] Dashboard parou inesperadamente. Reiniciando em 5s...
ping -n 6 127.0.0.1 >nul
goto DASHLOOP
