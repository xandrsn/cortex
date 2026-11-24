# Compila las dos apps de Cortex (onedir) y sus instaladores:
#   Cortex Hub   -> dist\CortexHub, dist\CortexHubSetup.exe   (voz, panel, LLM)
#   Cortex Sense -> dist\CortexSense, dist\CortexSenseSetup.exe (webcam: gestos -> comandos)
# -Only Hub|Sense compila solo una.
# Requisito: .venv con requirements.txt instalado (ver README.md).
# -Bridge: carpeta con whatsapp-bridge.exe y su codigo (main.go, go.mod, go.sum). Sin ella, el instalador no ofrece WhatsApp.
param([string]$Version = "0.3.0", [string]$Bridge = "", [ValidateSet("", "Hub", "Sense")][string]$Only = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "$PSScriptRoot\.venv\Scripts\python.exe"

# Modelos base del detector de "Cortex": pip no los trae (['-'] = solo los base, ninguna palabra de serie)
& $py -c "import openwakeword.utils as u; u.download_models(['-'])"
if ($LASTEXITCODE -ne 0) { throw "No se han podido descargar los modelos de openwakeword" }

$common = @("--noconfirm", "--clean", "--onedir", "--noconsole", "--paths", "src", "--distpath", "dist",
            "--workpath", "build", "--specpath", "build", "--exclude-module", "tkinter", "--collect-submodules", "uvicorn")

if ($Only -ne "Sense") {
    & $py -m PyInstaller @common --name CortexHub `
        --add-data "$PSScriptRoot\src\hub\silero_vad.onnx;hub" `
        --collect-submodules hub --collect-data openwakeword --exclude-module mediapipe --exclude-module pygame `
        --copy-metadata onnx-asr --copy-metadata faster-whisper --copy-metadata openwakeword --copy-metadata piper-tts --copy-metadata edge-tts `
        cortex_hub.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (Hub) fallo" }
    # El panel va junto al .exe: el codigo lo busca en <carpeta del .exe>\web\hub
    New-Item -ItemType Directory -Force "$PSScriptRoot\dist\CortexHub\web" | Out-Null
    Copy-Item -Recurse -Force "$PSScriptRoot\web\hub" "$PSScriptRoot\dist\CortexHub\web\hub"
    Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\CortexHub\README.md"
}
if ($Only -ne "Hub") {
    & $py -m PyInstaller @common --name CortexSense `
        --add-data "$PSScriptRoot\src\sense\window\hand_landmarker.task;sense\window" `
        --collect-submodules sense --collect-all mediapipe `
        cortex_sense.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (Sense) fallo" }
    New-Item -ItemType Directory -Force "$PSScriptRoot\dist\CortexSense\web" | Out-Null
    Copy-Item -Recurse -Force "$PSScriptRoot\web\sense" "$PSScriptRoot\dist\CortexSense\web\sense"
    Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\CortexSense\README.md"
}

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
$apps = [ordered]@{ Hub = "cortex-hub.iss"; Sense = "cortex-sense.iss" }
foreach ($name in $apps.Keys) {
    if ($Only -and $Only -ne $name) { continue }
    $d = if ($name -eq "Hub") { $defs } else { @("/DAppVersion=$Version") }
    & "$inno\tools\ISCC.exe" /Q @d "$PSScriptRoot\installer\$($apps[$name])"
    if ($LASTEXITCODE -ne 0) { throw "ISCC ($name) fallo" }
    $out = Get-Item "$PSScriptRoot\dist\Cortex${name}Setup.exe"
    "Listo: $out ($([math]::Round($out.Length / 1MB, 1)) MB) SHA256 $((Get-FileHash $out).Hash)"
}
