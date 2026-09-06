; installer.iss
;
; Inno Setup script for P2P Transfer.
; Compile with the Inno Setup IDE, or from the command line with:
;     iscc installer.iss
; (run from inside the packaging/ folder, or adjust the Source path
; below to match wherever you actually invoke it from)
;
; This produces a single installer .exe that:
;   1. Installs the PyInstaller --onedir build to Program Files
;   2. Checks for the WebView2 runtime, offering to install it if missing
;   3. Pre-configures a Windows Firewall exception for the app, rather
;      than relying on the reactive runtime prompt
;   4. Creates a Start Menu shortcut and a proper uninstaller
;
; HONEST CAVEAT, upfront: Inno Setup is a Windows-only tool. Nothing in
; this file has been compiled or run - I cannot do that from this
; sandbox at all, the same way I couldn't test pywebview itself
; directly. This is a careful, best-effort attempt based on well-
; established Inno Setup patterns, but needs real verification on an
; actual Windows machine before being trusted for real distribution.
; Specific things flagged below as needing that verification, not just
; assumed correct.

#define MyAppName "P2P Transfer"
#define MyAppVersion "1.0"
#define MyAppExeName "P2PTransfer.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputBaseFilename=P2PTransferSetup
Compression=lzma2
SolidCompression=yes
; Admin rights are required here specifically for the firewall rule
; below (which needs elevation) - not strictly needed for a per-user
; Program Files install alone, but requiring it upfront gives a single
; clean elevation prompt rather than two separate ones.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

[Files]
; The whole PyInstaller --onedir output folder, copied recursively.
; ADJUST THIS PATH if your actual build output location differs - this
; assumes you're compiling this .iss from the project root with the
; PyInstaller build already having run (dist/P2PTransfer/ exists).
Source: "..\dist\P2PTransfer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; The WebView2 bootstrapper - download this yourself from Microsoft
; first (see BUILD.md for the exact link) and place it in this same
; packaging/ folder before compiling. "dontcopy" means it's bundled
; INSIDE the installer's own data but not automatically extracted to
; the install folder - only pulled out to a temp location if the
; WebView2 check below determines it's actually needed.
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: dontcopy

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; Pre-configure the firewall exception during install, rather than
; relying on the reactive Windows Firewall prompt the first time the
; app actually tries to listen. This allows inbound connections to the
; app's executable specifically - not tied to one fixed port, since
; the person can change their listening port inside the app itself.
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""{#MyAppName}"" dir=in action=allow program=""{app}\{#MyAppExeName}"" enable=yes"; Flags: runhidden

; Offer to launch the app right after install finishes.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Clean up the firewall rule on uninstall too, so it doesn't linger
; on the system after the app itself is gone.
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden

[Code]
// Checks for the WebView2 runtime via its registry key. This specific
// GUID ({F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}) is a widely-documented
// standard check used by many WebView2-dependent installers - but
// since I can't inspect a real Windows registry from this sandbox,
// please verify this genuinely detects WebView2's presence (and,
// separately, its ABSENCE) correctly on an actual machine before
// relying on it for real distribution.
function IsWebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    or RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
end;

procedure InitializeWizard();
var
  ResultCode: Integer;
begin
  if not IsWebView2Installed() then
  begin
    if MsgBox('This app requires the Microsoft Edge WebView2 Runtime, which does not appear to be installed. Install it now?', mbConfirmation, MB_YESNO) = IDYES then
    begin
      ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
      Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
