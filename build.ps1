# Compila Cortex en dist\Cortex (onedir) y lo empaqueta en dist\Cortex-windows.zip.
# Requisito: .venv con requirements.txt instalado (ver README.md).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "$PSScriptRoot\.venv\Scripts\python.exe"

& $py -m PyInstaller --noconfirm --clean --onedir --console --name Cortex `
    --paths src --distpath dist --workpath build --specpath build `
    --add-data "$PSScriptRoot\src\gesture\hand_landmarker.task;gesture" `
    --collect-submodules pepito --collect-submodules uvicorn `
    --exclude-module tkinter --collect-all mediapipe --collect-submodules gesture `
    --copy-metadata onnx-asr --copy-metadata faster-whisper --copy-metadata openwakeword --copy-metadata piper-tts --copy-metadata edge-tts `
    cortex.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo" }

# El panel (web/) va junto al .exe: el código lo busca en la carpeta del .exe
Copy-Item -Recurse -Force "$PSScriptRoot\web" "$PSScriptRoot\dist\Cortex\web"
Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\Cortex\README.md"

$zip = "$PSScriptRoot\dist\Cortex-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$PSScriptRoot\dist\Cortex" -DestinationPath $zip
"Listo: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
