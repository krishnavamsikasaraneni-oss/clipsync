@echo off
REM Builds both apps on your own Windows PC (alternative to GitHub Actions).
REM Needs Python 3.12 from python.org installed. Double-click to run.
cd /d "%~dp0"
python -m pip install --upgrade pip || goto :fail
python -m pip install -r requirements.txt pyinstaller huggingface_hub || goto :fail
if not exist models\base.en\model.bin python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-base.en', local_dir='models/base.en')" || goto :fail
pyinstaller --noconfirm --windowed --name ClipSyncSender --collect-submodules zeroconf sender_app.py || goto :fail
pyinstaller --noconfirm --windowed --name ClipSyncReceiver --collect-submodules zeroconf --collect-all faster_whisper --collect-all ctranslate2 --collect-binaries onnxruntime --collect-all _sounddevice_data --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 receiver_app.py || goto :fail
xcopy /E /I /Y models\base.en dist\ClipSyncReceiver\models\base.en >nul
copy /Y SETUP.md dist\ClipSyncSender\ >nul
copy /Y SETUP.md dist\ClipSyncReceiver\ >nul
echo.
echo Done. Copy dist\ClipSyncSender to PC 1 and dist\ClipSyncReceiver to PC 2.
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
