@echo off
rem Crea l'eseguibile per Windows di SQL Profiler e lo copia in una cartella sul Desktop.
rem Da avviare con doppio clic o dal Prompt dei comandi:  crea_eseguibili.bat
rem Serve Node.js (https://nodejs.org) e internet la prima volta. Su Linux usare crea_eseguibili.sh.
rem Il computer su cui si usa l'app ha comunque bisogno di Python 3.10 o piu' recente.
setlocal
cd /d "%~dp0electron"

where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js non e' installato: scaricalo da https://nodejs.org e riprova.
  pause
  exit /b 1
)

rem npm ci ricrea node_modules: serve anche se la cartella e' stata copiata da Linux
if not exist "node_modules\electron\dist\electron.exe" (
  echo == Preparazione ^(la prima volta scarica gli strumenti, qualche minuto^)
  call npm ci --no-audit --no-fund
  if errorlevel 1 goto errore
)

echo == Creazione dell'eseguibile ^(puo' richiedere circa 10 minuti^)
call npx electron-builder --win portable
if errorlevel 1 goto errore

for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%d"
set "DEST=%DESKTOP%\SQL Profiler - eseguibili"
if not exist "%DEST%" mkdir "%DEST%"
copy /y "dist\SQL-Profiler-*.exe" "%DEST%\" >nul
if errorlevel 1 goto errore

echo.
echo ============================================================
echo   OK - ESEGUIBILE PRONTO
echo ============================================================
echo Cartella: %DEST%
dir /b "%DEST%"
echo.
pause
exit /b 0

:errore
echo.
echo [ERRORE] Creazione non riuscita: guarda i messaggi qui sopra.
echo Se l'errore parla di "symbolic link", attiva la Modalita' sviluppatore di Windows
echo oppure avvia questo file come amministratore.
pause
exit /b 1
