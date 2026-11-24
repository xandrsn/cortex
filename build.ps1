# Builds the two Cortex apps (onedir) and their installers:
#   Cortex Hub   -> dist\CortexHub, dist\CortexHubSetup.exe   (voice, panel, LLM)
#   Cortex Sense -> dist\CortexSense, dist\CortexSenseSetup.exe (webcam: gestures -> commands)
# -Only Hub|Sense builds just one.
# Requirement: .venv with requirements.txt installed (see README.md).
# -Bridge: folder with whatsapp-bridge.exe and its source (main.go, go.mod, go.sum). Without it, the installer does not offer WhatsApp.
param([string]$Version = "0.3.0", [string]$Bridge = "", [ValidateSet("", "Hub", "Sense")][string]$Only = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = "$PSScriptRoot\.venv\Scripts\python.exe"

# Base models of the "Cortex" detector: pip does not ship them (['-'] = only the base ones, no stock wake words)
& $py -c "import openwakeword.utils as u; u.download_models(['-'])"
if ($LASTEXITCODE -ne 0) { throw "Could not download the openwakeword models" }

$common = @("--noconfirm", "--clean", "--onedir", "--noconsole", "--paths", "src", "--distpath", "dist",
            "--workpath", "build", "--specpath", "build", "--exclude-module", "tkinter", "--collect-submodules", "uvicorn")

if ($Only -ne "Sense") {
    & $py -m PyInstaller @common --name CortexHub `
        --add-data "$PSScriptRoot\src\hub\silero_vad.onnx;hub" `
        --collect-submodules hub --collect-data openwakeword --exclude-module mediapipe --exclude-module pygame `
        --copy-metadata onnx-asr --copy-metadata faster-whisper --copy-metadata openwakeword --copy-metadata piper-tts --copy-metadata edge-tts `
        cortex_hub.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (Hub) failed" }
    # The panel ships next to the .exe: the code looks for it in <exe folder>\web\hub
    New-Item -ItemType Directory -Force "$PSScriptRoot\dist\CortexHub\web" | Out-Null
    Copy-Item -Recurse -Force "$PSScriptRoot\web\hub" "$PSScriptRoot\dist\CortexHub\web\hub"
    Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\CortexHub\README.md"
}
if ($Only -ne "Hub") {
    & $py -m PyInstaller @common --name CortexSense `
        --add-data "$PSScriptRoot\src\sense\window\hand_landmarker.task;sense\window" `
        --collect-submodules sense --collect-all mediapipe `
        cortex_sense.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (Sense) failed" }
    New-Item -ItemType Directory -Force "$PSScriptRoot\dist\CortexSense\web" | Out-Null
    Copy-Item -Recurse -Force "$PSScriptRoot\web\sense" "$PSScriptRoot\dist\CortexSense\web\sense"
    Copy-Item -Force "$PSScriptRoot\README.md" "$PSScriptRoot\dist\CortexSense\README.md"
}

# Inno Setup, pinned by version and hash
$inno = "$PSScriptRoot\build\inno-6.7.3"
if (-not (Test-Path "$inno\tools\ISCC.exe")) {
    $zip = "$PSScriptRoot\build\inno.zip"
    [Net.ServicePointManager]::SecurityProtocol = "Tls12"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -UseBasicParsing "https://api.nuget.org/v3-flatcontainer/tools.innosetup/6.7.3/tools.innosetup.6.7.3.nupkg" -OutFile $zip
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne "f780898e402ff80612cc8d9fcb8c6e02932bd1cb4c900ffdaa31f9341cfb49f4") { throw "The Inno Setup hash does not match" }
    Expand-Archive $zip $inno
}
$defs = @("/DAppVersion=$Version")
if ($Bridge) {
    # Closed list: that folder also holds store\ (personal messages), which is never copied
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
    [string[]]$d = if ($name -eq "Hub") { $defs } else { "/DAppVersion=$Version" }
    & "$inno\tools\ISCC.exe" /Q $d "$PSScriptRoot\installer\$($apps[$name])"
    if ($LASTEXITCODE -ne 0) { throw "ISCC ($name) failed" }
    $out = Get-Item "$PSScriptRoot\dist\Cortex${name}Setup.exe"
    "Done: $out ($([math]::Round($out.Length / 1MB, 1)) MB) SHA256 $((Get-FileHash $out).Hash)"
}
