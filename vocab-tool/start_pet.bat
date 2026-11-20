@echo off
rem ---------------------------------------------------------------------
rem  Double-click to start the local server and open the workbench page.
rem  ASCII only on purpose: cmd reads a BOM-less file as ANSI, so any
rem  non-ASCII character here could break the quoting.
rem
rem  --no-browser  start the server only, do not open a browser (for gates)
rem ---------------------------------------------------------------------
setlocal
set "PORT=8901"
set "DIR=%~dp0"

rem ---- locate python: portable runtime -> local override -> PATH --------
rem  1) dist package: runtime\python.exe sits next to the vocab-tool folder
rem  2) dev machine : vocab-tool\local_python.txt holds an absolute path
rem  3) fall back   : whatever "python" resolves to on PATH
set "PY=%~dp0..\runtime\python.exe"
if exist "%PY%" goto havepy
set "PY="
if exist "%~dp0local_python.txt" for /f "usebackq delims=" %%P in ("%~dp0local_python.txt") do set "PY=%%P"
if not defined PY goto usepath
if exist "%PY%" goto havepy

:usepath
set "PY=python"
where python >nul 2>&1
if errorlevel 1 goto nopy

:havepy

::checkport
rem Something already listening? It might be an OLD copy of this app, or a
rem completely different program that happens to use the same port.
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 goto start

call :probe
if "%OK%"=="1" goto reuse
echo ERROR: port %PORT% is taken, but it is NOT the VocabSpeaker server.
echo        (/api/packs did not answer 200 - see the diagnostic above)
echo        Close that program, or change PORT in this file (e.g. 8903).
rem keep the window open on double-click, otherwise the error flashes by
rem and the user reports "nothing happened"
if /i not "%~1"=="--no-browser" pause
exit /b 1

:reuse
echo Port %PORT% is already serving VocabSpeaker - reusing it.
goto open

:start
echo Starting server on port %PORT% ...
start "VocabSpeaker server" /min "%PY%" "%DIR%serve.py" %PORT%
rem ping is used instead of timeout: timeout dies when stdin is redirected
ping -n 4 127.0.0.1 >nul 2>&1
call :probe
if "%OK%"=="1" goto open
echo WARN: server did not answer /api/packs on port %PORT%.
echo       Check: PY path valid? port taken by another program? serve.py error?
if /i not "%~1"=="--no-browser" pause

:open
if /i "%~1"=="--no-browser" goto end
start "" "http://127.0.0.1:%PORT%/"

:end
exit /b 0

:nopy
echo ERROR: python interpreter not found. Tried:
echo        1) %~dp0..\runtime\python.exe   (portable package)
echo        2) %~dp0local_python.txt        (write one line: full path to python.exe)
echo        3) python on PATH
echo   Install Python 3.13 and re-run, or use the portable package.
if /i not "%~1"=="--no-browser" pause
exit /b 1

rem ---- probe: GET /api/packs, expect 200 -------------------------------
:probe
set "OK=0"
for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try{(Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:%PORT%/api/packs').StatusCode}catch{Write-Output 0}"`) do set "OK=%%R"
if "%OK%"=="200" set "OK=1"
goto :eof
