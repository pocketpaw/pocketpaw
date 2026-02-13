; PocketPaw Windows Installer — Inno Setup Script
; Reads version from POCKETPAW_VERSION env var (default "0.1.0").
;
; Usage:
;   set POCKETPAW_VERSION=0.3.0
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" pocketpaw.iss
;
; Or from CI:
;   iscc /DVERSION=0.3.0 pocketpaw.iss

#ifndef VERSION
  #define VERSION GetEnv("POCKETPAW_VERSION")
  #if VERSION == ""
    #define VERSION "0.1.0"
  #endif
#endif

[Setup]
AppId={{B7E3F4A2-8C1D-4E5F-9A0B-6D2C7E8F1A3B}
AppName=PocketPaw
AppVersion={#VERSION}
AppVerName=PocketPaw {#VERSION}
AppPublisher=PocketPaw
AppPublisherURL=https://github.com/pocketpaw/pocketpaw
DefaultDirName={localappdata}\PocketPaw
DefaultGroupName=PocketPaw
OutputDir=..\..\..\dist\launcher
OutputBaseFilename=PocketPaw-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\PocketPaw.exe
WizardStyle=modern
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\..\dist\launcher\PocketPaw\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PocketPaw"; Filename: "{app}\PocketPaw.exe"
Name: "{autodesktop}\PocketPaw"; Filename: "{app}\PocketPaw.exe"; Tasks: desktopicon
Name: "{userstartup}\PocketPaw"; Filename: "{app}\PocketPaw.exe"; Tasks: autostart

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "autostart"; Description: "Start PocketPaw when Windows starts"; GroupDescription: "Startup:"

[Run]
Filename: "{app}\PocketPaw.exe"; Description: "Launch PocketPaw"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir: String;
  Res: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    ConfigDir := ExpandConstant('{userappdata}') + '\.pocketclaw';
    // Use USERPROFILE for ~/.pocketclaw on Windows
    ConfigDir := GetEnv('USERPROFILE') + '\.pocketclaw';
    if DirExists(ConfigDir) then
    begin
      Res := MsgBox(
        'Do you want to remove PocketPaw configuration and data?' + #13#10 +
        '(Located at: ' + ConfigDir + ')' + #13#10#13#10 +
        'Click Yes to remove everything, No to keep your data.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2
      );
      if Res = IDYES then
      begin
        DelTree(ConfigDir, True, True, True);
      end;
    end;
  end;
end;
