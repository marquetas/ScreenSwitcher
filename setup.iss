#define MyAppName "ScreenSwitcher"
#define MyAppVersion "1.1.0"
#define MyAppExeName "main.exe"

[Setup]
AppId={{F8FCA0C1-3E2C-4E77-8B24-2D7C2A0B7F44}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=setup
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableDirPage=no
DisableProgramGroupPage=no
AppendDefaultDirName=yes
LanguageDetectionMethod=locale
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter={#MyAppExeName}

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear icono en el escritorio"; Flags: unchecked
Name: "launch"; Description: "Abrir ScreenSwitcher al terminar"; Flags: unchecked

[Files]
Source: "dist\main.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ScreenSwitcher"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\ScreenSwitcher"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\Desinstalar ScreenSwitcher"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Flags: nowait postinstall skipifsilent; Tasks: launch

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName} /T"; Flags: runhidden waituntilterminated
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v {#MyAppName} /f"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\ScreenSwitcher"