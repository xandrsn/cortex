# Compila Cortex en dist\Cortex (onedir) y el instalador en dist\CortexSetup.exe.
# Requisito: .venv con requirements.txt instalado (ver README.md).
# -Bridge: carpeta con whatsapp-bridge.exe y su codigo (main.go, go.mod, go.sum). Sin ella, el instalador no ofrece WhatsApp.
param([string]$Version = "0.2.0", [string]$Bridge = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "$PSScriptRoot\.venv\Scripts\python.exe"

# Modelos base del detector de "Cortex": pip no los trae (['-'] = solo los base, ninguna palabra de serie)
& $py -c "import openwakeword.utils as u; u.download_models(['-'])"
if ($LASTEXITCODE -ne 0) { throw "No se han podido descargar los modelos de openwakeword" }

& $py -m PyInstaller --noconfirm --clean --onedir --noconsole --name Cortex `
    --paths src --distpath dist --workpath build --specpath build `
    --add-data "$PSScriptRoot\src\gesture\hand_landmarker.task;gesture" `
    --add-data "$PSScriptRoot\src\pepito\silero_vad.onnx;pepito" `
    --collect-submodules pepito --collect-submodules uvicorn `
    --exclude-module tkinter --collect-all mediapipe --collect-submodules gesture --collect-data openwakeword `
    --copy-metadata onnx-asr --copy-metadata faster-whisper --copy-metadata openwakeword --copy-metadata piper-tts --copy-metadata edge-tts `
    cortex.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo" }

# El panel (web/) va junto al .exe: el codigo lo busca en la carpeta del .exe
Copy-Item -Recurse -Force "$PSScriptRoot\web" "$PSScriptRoot\dist\Cortex\web"
Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\Cortex\README.md"

# Inno Setup, fijado por version y hash
$inno = "$PSScriptRoot\build\inno-6.7.3"
if (-not (Test-Path "$inno\tools\ISCC.exe")) {
    $zip = "$PSScriptRoot\build\inno.zip"
    [Net.ServicePointManager]::SecurityProtocol = "Tls12"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -UseBasicParsing "https://api.nuget.org/v3-flatcontainer/tools.innosetup/6.7.3/tools.innosetup.6.7.3.nupkg" -OutFile $zip
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne "f780898e402ff80612cc8d9fcb8c6e02932bd1cb4c900ffdaa31f9341cfb49f4") { throw "El hash de Inno Setup no coincide" }
    Expand-Archive $zip $inno
}
$defs = @("/DAppVersion=$Version")
if ($Bridge) {
    # Lista cerrada: en esa carpeta tambien esta store\ (mensajes personales), que nunca se copia
    $stage = "$PSScriptRoot\build\bridge"
    New-Item -ItemType Directory -Force $stage | Out-Null
    foreach ($f in "whatsapp-bridge.exe", "main.go", "go.mod", "go.sum") { Copy-Item -Force "$Bridge\$f" "$stage\$f" }
    Copy-Item -Force "$Bridge\..\LICENSE" "$stage\LICENSE"
    Copy-Item -Force "$PSScriptRoot\installer\whatsapp-NOTICE.txt" "$stage\NOTICE.txt"
    $defs += "/DBridge=$stage"
}
& "$inno\tools\ISCC.exe" /Q @defs "$PSScriptRoot\installer\cortex.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC fallo" }
$out = Get-Item "$PSScriptRoot\dist\CortexSetup.exe"
"Listo: $out ($([math]::Round($out.Length / 1MB, 1)) MB) SHA256 $((Get-FileHash $out).Hash)"
