@echo off
title Automacao Documental - Gmail + Drive

:: ============================================================
::  CONFIGURACOES - ajuste se necessario
:: ============================================================
set PYTHON=python
set SCRIPT=main.py
set INTERVALO=10
:: INTERVALO em segundos entre cada execucao (padrao: 300 = 5 minutos)
:: ============================================================

echo.
echo  =============================================
echo   Automacao Documental - Gmail + Drive
echo   Loop a cada %INTERVALO%s  (Ctrl+C para parar)
echo  =============================================
echo.

:: Verifica se o Python esta disponivel
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Verifique se esta instalado e no PATH.
    pause
    exit /b 1
)

:: Verifica se o script principal existe
if not exist "%SCRIPT%" (
    echo [ERRO] Arquivo "%SCRIPT%" nao encontrado.
    echo        Execute este .bat na mesma pasta do projeto.
    pause
    exit /b 1
)

:: Verifica se o credentials.json existe
if not exist "credentials.json" (
    echo [AVISO] credentials.json nao encontrado.
    echo         O script vai falhar na autenticacao.
    pause
)

:: Verifica se o .env existe
if not exist ".env" (
    echo [AVISO] Arquivo .env nao encontrado.
    echo         Copie o env.example para .env e preencha as variaveis.
    pause
)

:LOOP
echo.
echo [%date% %time%] Iniciando execucao...
echo ---------------------------------------------

%PYTHON% %SCRIPT%

if errorlevel 1 (
    echo.
    echo [%date% %time%] Script terminou com erro. Aguardando %INTERVALO%s...
) else (
    echo.
    echo [%date% %time%] Concluido. Proxima execucao em %INTERVALO%s...
)

echo ---------------------------------------------
ping -n %INTERVALO% 127.0.0.1 >nul

goto LOOP
