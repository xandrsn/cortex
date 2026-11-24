; Instalador de Cortex Sense (Inno Setup 6). Lo compila build.ps1: ISCC /DAppVersion=x.y.z installer\cortex-sense.iss
; Por usuario y sin administrador: Sense guarda sense.yaml y los gestos propios en su propia carpeta.
; La primera vez que se abre, el panel (http://127.0.0.1:8766) lleva al asistente: cámara y calibración.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8A4AEECC-FBF7-4DD4-9361-8BBCA36E75BD}
AppName=Cortex Sense
AppVersion={#AppVersion}
AppPublisher=Charlie Andersen
AppPublisherURL=https://github.com/xandrsn/cortex
DefaultDirName={autopf}\Cortex Sense
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
ShowLanguageDialog=auto
CloseApplications=no
UninstallDisplayIcon={app}\CortexSense.exe
OutputDir=..\dist
OutputBaseFilename=CortexSenseSetup
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.Privacy=Cortex Sense uses your webcam to recognise hand gestures. Video is processed on this computer and never uploaded. The camera light stays on while gestures are enabled; you can turn them off from the Sense panel at any time.
es.Privacy=Cortex Sense usa la webcam para reconocer gestos de la mano. El vídeo se procesa en este equipo y nunca se sube. La luz de la cámara queda encendida mientras los gestos estén activados; se pueden apagar desde el panel de Sense.
en.TaskDesktop=Desktop shortcut
es.TaskDesktop=Acceso directo en el escritorio
en.TaskAutostart=Start Cortex Sense with Windows (camera on)
es.TaskAutostart=Abrir Cortex Sense al iniciar Windows (con la cámara)
en.RunNow=Open Cortex Sense and calibrate
es.RunNow=Abrir Cortex Sense y calibrar
en.NotStopped=Cortex Sense could not be closed. Close it and try again.
es.NotStopped=No se ha podido cerrar Cortex Sense. Ciérralo y vuelve a intentarlo.

[Tasks]
Name: "desktop"; Description: "{cm:TaskDesktop}"
Name: "autostart"; Description: "{cm:TaskAutostart}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\web"

[Files]
Source: "..\dist\CortexSense\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Cortex Sense"; Filename: "{app}\CortexSense.exe"; Parameters: "--open"
Name: "{autodesktop}\Cortex Sense"; Filename: "{app}\CortexSense.exe"; Parameters: "--open"; Tasks: desktop

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Cortex Sense"; ValueData: """{app}\CortexSense.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\CortexSense.exe"; Parameters: "--open"; Description: "{cm:RunNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\logs"
Type: dirifempty; Name: "{app}"

[Code]
var
  Privacy: TOutputMsgWizardPage;

procedure InitializeWizard;
begin
  Privacy := CreateOutputMsgPage(wpWelcome, 'Webcam', '', CustomMessage('Privacy'));
end;

{ Solo el CortexSense.exe de esta carpeta }
function StopSense: Boolean;
var
  Code: Integer;
  Dir: String;
begin
  Dir := ExpandConstant('{app}\');
  StringChangeEx(Dir, '''', '''''', True);
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "$p = @(Get-Process CortexSense -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ' +
    '$_.Path.StartsWith(''' + Dir + ''', ''OrdinalIgnoreCase'') }); if ($p) { $p | Stop-Process -Force -ErrorAction SilentlyContinue; ' +
    '$p | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue }; exit @($p | Where-Object { -not $_.HasExited }).Count"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopSense then Result := CustomMessage('NotStopped');
end;

function InitializeUninstall: Boolean;
begin
  Result := StopSense;
  if not Result and not UninstallSilent then MsgBox(CustomMessage('NotStopped'), mbError, MB_OK);
end;
