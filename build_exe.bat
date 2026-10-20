@echo off
REM Build ytmp3.exe - a standalone Windows executable with the GUI.
REM Run this again whenever you update yt-dlp (YouTube changes over time and a
REM frozen yt-dlp eventually stops working - "pip install -U yt-dlp" then rebuild).

echo [build] installing / updating dependencies...
python -m pip install -U yt-dlp imageio-ffmpeg pyinstaller || goto :error

REM Deno is the JS runtime yt-dlp uses to solve YouTube's challenges; it gets
REM bundled into the exe. Download it once if it isn't vendored yet.
if not exist "vendor\deno.exe" (
  echo [build] vendor\deno.exe not found - downloading Deno...
  if not exist "vendor" mkdir "vendor"
  powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
    "Invoke-WebRequest 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile 'vendor\deno.zip';" ^
    "Expand-Archive 'vendor\deno.zip' -DestinationPath 'vendor' -Force;" ^
    "Remove-Item 'vendor\deno.zip'" || goto :error
)

echo [build] building standalone exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ytmp3 ^
  --collect-all imageio_ffmpeg ^
  --collect-all yt_dlp ^
  --add-binary "vendor\deno.exe;." ^
  ytmp3_gui.py || goto :error

echo.
echo [build] done -^> dist\ytmp3.exe
goto :eof

:error
echo.
echo [build] FAILED - see the messages above.
exit /b 1
