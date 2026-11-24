; Cortex Hub installer (Inno Setup 6). Built by build.ps1: ISCC /DAppVersion=x.y.z [/DBridge=folder] installer\cortex-hub.iss
; Gestures ship separately: installer\cortex-sense.iss. AppId is the single-app version's: it updates in place.
; Per-user, no administrator: Cortex writes config.yaml, .env and the models in its own folder.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B6E5C-6B1B-4C0B-9C5B-5B7B0E6F2A41}
AppName=Cortex Hub
AppVersion={#AppVersion}
AppPublisher=Charlie Andersen
AppPublisherURL=https://github.com/xandrsn/cortex
DefaultDirName={autopf}\Cortex
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
ShowLanguageDialog=auto
CloseApplications=no
UninstallDisplayIcon={app}\CortexHub.exe
OutputDir=..\dist
OutputBaseFilename=CortexHubSetup
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
es.WidgetsTitle=Tus tarjetas
en.WidgetsTitle=Your widgets
es.WidgetsText=Marca las tarjetas que quieres en el panel. Se pueden cambiar cuando quieras.
en.WidgetsText=Choose the cards for your panel. You can change them at any time.
es.AccountsTitle=Tus cuentas
en.AccountsTitle=Your accounts
es.AccountsText=Marca lo que quieras conectar. El instalador no pide contraseñas: al abrir Cortex, un asistente te guía por cada inicio de sesión.%n%nEl asistente de voz deja el micro encendido mientras espera «Cortex»; ese audio se procesa en tu equipo y lo que le digas va a Claude.
en.AccountsText=Choose what to connect. The installer never asks for passwords: when Cortex opens, a wizard guides you through each login.%n%nThe voice assistant keeps the microphone on while it waits for "Cortex"; that audio is processed on your computer and what you say to it goes to Claude.
es.AccClaude=Claude (el asistente; necesita Claude Code y tu cuenta)
en.AccClaude=Claude (the assistant; needs Claude Code and your account)
es.AccSpotify=Spotify (música; con tu propia app de Spotify y Premium)
en.AccSpotify=Spotify (music; uses your own Spotify app and Premium)
es.AccEmail=Correo de Gmail (con contraseña de aplicación)
en.AccEmail=Gmail (with an app password)
es.AccWhatsApp=WhatsApp (cliente no oficial: WhatsApp podría limitar la cuenta)
en.AccWhatsApp=WhatsApp (unofficial client: WhatsApp may restrict the account)
es.WaTitle=Permiso de WhatsApp
en.WaTitle=WhatsApp permission
es.WaText=Cortex usa un cliente no oficial: WhatsApp podría limitar o bloquear la cuenta, y más si envías mensajes. El asistente nunca envía; solo tú, desde un chat abierto. Se puede cambiar en Ajustes.
en.WaText=Cortex uses an unofficial client: WhatsApp may restrict or ban the account, more so if you send messages. The assistant never sends; only you do, from an open chat. You can change this in Settings.
es.WaRead=Solo leer
en.WaRead=Read only
es.WaWrite=Leer y escribir
en.WaWrite=Read and write
es.TaskDesktop=Acceso directo en el escritorio
en.TaskDesktop=Desktop shortcut
es.TaskAutostart=Abrir Cortex al iniciar Windows (en segundo plano, con el micro activo)
en.TaskAutostart=Start Cortex with Windows (in the background, microphone on)
es.RunNow=Abrir Cortex
en.RunNow=Open Cortex
es.DataKept=Cortex se ha desinstalado. Tus datos (ajustes, claves, mensajes vinculados) siguen en:%n%n%1%n%nBorra esa carpeta si no los quieres conservar.
en.DataKept=Cortex was uninstalled. Your data (settings, keys, linked messages) is still in:%n%n%1%n%nDelete that folder if you do not want to keep it.
es.NotStopped=No se ha podido cerrar Cortex. Ciérralo (Ajustes → Cerebro → Cerrar Cortex) y vuelve a intentarlo.
en.NotStopped=Cortex could not be closed. Close it (Settings → Brain → Quit Cortex) and try again.

[Tasks]
Name: "desktop"; Description: "{cm:TaskDesktop}"
Name: "autostart"; Description: "{cm:TaskAutostart}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\web"
Type: files; Name: "{app}\Cortex.exe"

[Files]
Source: "..\dist\CortexHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef Bridge
Source: "{#Bridge}\*"; DestDir: "{app}\whatsapp-mcp\whatsapp-bridge"; Flags: ignoreversion; Check: WantsWhatsApp
#endif

[Icons]
Name: "{autoprograms}\Cortex Hub"; Filename: "{app}\CortexHub.exe"; Parameters: "--open"
Name: "{autodesktop}\Cortex Hub"; Filename: "{app}\CortexHub.exe"; Parameters: "--open"; Tasks: desktop

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Cortex"; ValueData: """{app}\CortexHub.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\CortexHub.exe"; Parameters: "--open"; Description: "{cm:RunNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; only what is re-downloaded or regenerated; user data is never touched
Type: filesandordirs; Name: "{app}\models\hf"
Type: filesandordirs; Name: "{app}\logs"
Type: dirifempty; Name: "{app}\models"
Type: dirifempty; Name: "{app}"

[Code]
var
  Widgets, Accounts: TInputOptionWizardPage;
#ifdef Bridge
  WaMode: TInputOptionWizardPage;
#endif
  Ids: TStringList;
  Fresh: Boolean;

procedure AddWidget(Id, Es, En: String; On: Boolean);
begin
  Ids.Add(Id);
  if ActiveLanguage = 'es' then Widgets.Add(Es) else Widgets.Add(En);
  Widgets.Values[Ids.Count - 1] := On;
end;

procedure InitializeWizard;
begin
  Ids := TStringList.Create;
  Widgets := CreateInputOptionPage(wpSelectDir, CustomMessage('WidgetsTitle'), '', CustomMessage('WidgetsText'), False, True);
  { same ids as WIDGETS in web/hub/app.js (tests/test_installer.py checks it) }
  AddWidget('today', 'Hoy', 'Today', True);
  AddWidget('tasks', 'Tareas', 'Tasks', True);
  AddWidget('avisos', 'Avisos', 'Reminders', True);
  AddWidget('timers', 'Temporizadores', 'Timers', True);
  AddWidget('compra', 'Compra', 'Shopping list', True);
  AddWidget('mail', 'Correo', 'Mail', True);
  AddWidget('whatsapp', 'WhatsApp', 'WhatsApp', False);
  AddWidget('search', 'Vídeos', 'Videos', True);
  AddWidget('devices', 'Dispositivos', 'Devices', True);
  AddWidget('meals', 'Comidas', 'Meals', True);
  AddWidget('gym', 'Gym', 'Gym', True);
  AddWidget('money', 'Dinero', 'Money', False);
  AddWidget('betis', 'Betis', 'Betis football', False);
  AddWidget('gestures', 'Gestos', 'Gestures', False);
  AddWidget('status', 'Estado', 'Status', False);

  Accounts := CreateInputOptionPage(Widgets.ID, CustomMessage('AccountsTitle'), '', CustomMessage('AccountsText'), False, False);
  Accounts.Add(CustomMessage('AccClaude'));
  Accounts.Add(CustomMessage('AccSpotify'));
  Accounts.Add(CustomMessage('AccEmail'));
#ifdef Bridge
  Accounts.Add(CustomMessage('AccWhatsApp'));
  WaMode := CreateInputOptionPage(Accounts.ID, CustomMessage('WaTitle'), '', CustomMessage('WaText'), True, False);
  WaMode.Add(CustomMessage('WaRead'));
  WaMode.Add(CustomMessage('WaWrite'));
  WaMode.SelectedValueIndex := 0;
#endif
  Accounts.Values[0] := True;
end;

{ On update, existing settings are kept }
function FirstInstall: Boolean;
begin
  Result := not FileExists(ExpandConstant('{app}\config.yaml'));
end;

function WantsWhatsApp: Boolean;
begin
#ifdef Bridge
  Result := Accounts.Values[3];
#else
  Result := False;
#endif
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = Widgets.ID) and not FirstInstall;
#ifdef Bridge
  if PageID = WaMode.ID then Result := not (WantsWhatsApp and FirstInstall);
#endif
end;

{ Only processes from this folder: another WhatsApp bridge not belonging to Cortex may be running }
function StopCortex: Boolean;
var
  Code: Integer;
  Dir: String;
begin
  Dir := ExpandConstant('{app}\');
  StringChangeEx(Dir, '''', '''''', True);
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "$p = @(Get-Process Cortex,CortexHub,whatsapp-bridge -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ' +
    '$_.Path.StartsWith(''' + Dir + ''', ''OrdinalIgnoreCase'') }); if ($p) { $p | Stop-Process -Force -ErrorAction SilentlyContinue; ' +
    '$p | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue }; exit @($p | Where-Object { -not $_.HasExited }).Count"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  Fresh := FirstInstall;
  if not StopCortex then Result := CustomMessage('NotStopped');
end;

function ListOf(Page: TInputOptionWizardPage; Names: TStringList): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to Names.Count - 1 do
    if Page.Values[I] then begin
      if Result <> '' then Result := Result + ', ';
      Result := Result + Names[I];
    end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Names: TStringList;
  Mode: String;
begin
  if (CurStep <> ssPostInstall) or not Fresh then Exit;
  Names := TStringList.Create;
  Names.Add('claude');
  Names.Add('spotify');
  Names.Add('email');
#ifdef Bridge
  Names.Add('whatsapp');
#endif
  Mode := 'read';
#ifdef Bridge
  if WantsWhatsApp and (WaMode.SelectedValueIndex = 1) then Mode := 'write';
#endif
  SaveStringToFile(ExpandConstant('{app}\config.yaml'),
    'whatsapp_mode: ' + Mode + #13#10 +
    'setup_widgets: [' + ListOf(Widgets, Ids) + ']' + #13#10 +
    'setup_accounts: [' + ListOf(Accounts, Names) + ']' + #13#10, False);
  Names.Free;
end;

function InitializeUninstall: Boolean;
begin
  Result := StopCortex;
  if not Result and not UninstallSilent then MsgBox(CustomMessage('NotStopped'), mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}')) and not UninstallSilent then
    MsgBox(FmtMessage(CustomMessage('DataKept'), [ExpandConstant('{app}')]), mbInformation, MB_OK);
end;
