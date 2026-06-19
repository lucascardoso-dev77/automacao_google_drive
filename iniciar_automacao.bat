@echo off
title Automacao Documental - Gmail + Drive

:: ============================================================
::  CONFIGURACOES - ajuste se necessario
:: ============================================================
set PYTHON=py -3.12
set SCRIPT=main.py
set INTERVALO=8
:: INTERVALO em segundos entre cada execucao (padrao: 300 = 5 minutos)
:: ============================================================

echo.
echo  =============================================
echo   Automacao Documental - Gmail + Drive
echo   Loop a cada %INTERVALO%s  (Ctrl+C para parar)
echo   Dashboard: http://localhost:5000
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

:: Sobe o dashboard em janela separada, mantido vivo o tempo todo
:: (independente dos reinicios do loop de automacao abaixo)
if exist "iniciar_dashboard.bat" (
    echo Iniciando dashboard em janela separada...
    start "Dashboard - Automacao Documental" cmd /k "iniciar_dashboard.bat"

    :: Aguarda 2s para o servidor subir antes de abrir o navegador
    ping -n 3 127.0.0.1 >nul
    start http://localhost:5000
) else (
    echo [AVISO] iniciar_dashboard.bat nao encontrado. Dashboard nao sera iniciado.
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
:: Aguarda INTERVALO segundos antes do proximo ciclo
:: (ping -n N = N-1 segundos de espera real)
set /a PING_COUNT=%INTERVALO%+1
ping -n %PING_COUNT% 127.0.0.1 >nul

goto LOOP
