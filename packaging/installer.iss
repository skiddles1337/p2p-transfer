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
;   2. Pre-configures a Windows Firewall exception for the app, rather
;      than relying on the reactive runtime prompt
;   3. Creates a Start Menu shortcut and a proper uninstaller
;
; Does NOT automatically check for/install the WebView2 runtime - an
; earlier version tried this and got it wrong on a real test (see the
; note further down for what happened and why it was removed).
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

; WebView2 detection was removed after real-world testing: the
; registry-key check incorrectly reported "not installed" on a machine
; that already had WebView2 (which is the COMMON case - it ships with
; Windows 11, and reaches most Windows 10 machines via normal
; updates), triggering an unnecessary bootstrapper run that showed a
; confusing "already installed" error dialog to what should have been
; a clean install for most people. Rather than keep guessing at
; registry specifics that can't be verified without a real Windows
; machine to test against, this is intentionally left out - the rare
; person who genuinely lacks WebView2 can be pointed to installing it
; manually (see README.md), rather than risking this exact failure
; mode for the majority who already have it.
