; =====================================================================
; AlterChat - Inno Setup Script
; Packages PyInstaller --onedir output into a professional Windows Installer
; =====================================================================

#define MyAppName "AlterChat"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "AlterChat"
#define MyAppExeName "AlterChat.exe"
#define MySourceDir "dist\AlterChat"

[Setup]
; Unique application GUID for clean upgrades and uninstalls
AppId={{E680E1C9-7561-4996-98BC-2E47E64B7DC1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=AI WhatsApp Persona Chatbot
AppCopyright=Copyright (C) 2026 AlterChat

; Install to Local AppData (avoids admin rights issues when saving .env & history)
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Output configuration
OutputDir=installer_output
OutputBaseFilename=AlterChat_Setup_v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes

; Modern UI Style
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Main application executable
Source: "{#MySourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Python internal runtime, packages, and DLLs
Source: "{#MySourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; User data folder (avatars, history) - onlyifdoesntexist prevents wiping user data on update
Source: "{#MySourceDir}\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist

; Environment config (.env) - onlyifdoesntexist preserves user's configured API key on update
Source: "{#MySourceDir}\.env"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist skipifsourcedoesntexist
Source: "{#MySourceDir}\.env.example"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; Documentation
Source: "{#MySourceDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; Ensure data subfolders exist
Name: "{app}\data"
Name: "{app}\data\avatars"
Name: "{app}\data\history"

[Icons]
; Start Menu shortcut
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional task)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Checkbox to launch AlterChat immediately after setup finishes
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

