# Building the Windows Distribution

This describes turning the source code into a real, installable
Windows app - the original goal from the very start of this project
(see `docs/DESIGN.md` §1: "a normal Windows user with no Python
installed and no interest in a terminal").

**Important, upfront:** none of this has been run or tested - both
PyInstaller and Inno Setup are tools that build/verify on the actual
target OS, and this whole project has been developed from a Linux
sandbox with no way to run either one, or to test a real Windows
executable at all. Everything here is a careful, best-effort attempt
based on well-established patterns for each tool - but this is
genuinely the first time any of it will actually run, on your machine.
Expect this to take a few iterations, the same way the `pywebview`-
specific experiments earlier in this project did.

## What gets packaged

Only `gui_app.py` (the GUI) - not `cli.py`. The distributed installer's
whole point is reaching someone who doesn't want a terminal at all;
`cli.py` remains a run-from-source tool for anyone comfortable with
that.

## Step 1: Install PyInstaller

```
pip install pyinstaller
```

## Step 2: Build the app folder

From the **project root** (not from inside `packaging/`):

```
pyinstaller packaging/p2p_transfer.spec
```

This should produce `dist/P2PTransfer/` containing `P2PTransfer.exe`
and everything it needs alongside it.

(Confirmed via a real build attempt: PyInstaller resolves this spec
file's paths relative to the spec file's own folder, not your current
directory - the spec file already accounts for this, so running the
command as written above should just work.)

## Step 3: Test the built .exe directly, before wrapping it in an installer

```
cd dist\P2PTransfer
P2PTransfer.exe
```

Confirm the actual window opens and works - the real GUI, not just
"it built without error." If it fails to start, or crashes trying to
find `index.html`, check:
- Whether `dist\P2PTransfer\gui\index.html` actually exists (confirms
  whether the `datas` bundling in the spec file worked)
- Any "no module named webview.platforms.X" error - this means the
  `hiddenimports` list in the spec file needs another entry; the spec
  file's own comments explain why this can happen

**Confirmed via a real test:** `P2PTransfer.exe` genuinely needs its
companion `_internal\` folder present alongside it - moving just the
`.exe` alone breaks it, moving the whole `dist\P2PTransfer\` folder
together works correctly. This is normal, expected `--onedir` behavior
(newer PyInstaller versions bundle most files into `_internal\` rather
than scattering them directly next to the exe) - not a bug, and not
something anyone using the final installed app needs to think about,
since `installer.iss` already copies the entire folder wholesale (see
its `[Files]` section) rather than singling out the `.exe` alone.

## Step 4: Install Inno Setup

Download from: https://jrsoftware.org/isinfo.php

## Step 5: Compile the installer

Open `packaging/installer.iss` in the Inno Setup IDE and compile (or
run `iscc installer.iss` from inside `packaging/` if using the command
line). This should produce `packaging/Output/P2PTransferSetup.exe`.

## Step 6: Test the installer itself - thoroughly

This is the part most worth being careful about, since none of it has
ever run before:

- Does it actually install without errors?
- After install, does the app launch correctly from the Start Menu
  shortcut?
- Check Windows Firewall's settings (Windows Defender Firewall →
  Advanced Settings → Inbound Rules) - is there actually a new rule
  for "P2P Transfer"? Try listening on a port and connecting from
  another machine *without* manually approving any firewall prompt -
  if the pre-configured rule genuinely works, no prompt should appear
  at all.
- Does uninstalling cleanly remove the app AND the firewall rule (recheck
  the firewall rules list after uninstalling)?

## Known, accepted limitations (not bugs to fix)

- **No automatic WebView2 check.** An earlier version tried this and
  got it wrong on a real test - see `installer.iss`'s own comments for
  what happened. Most machines already have WebView2 (it ships with
  Windows 11, and reaches most Windows 10 machines via normal
  updates); the rare person who doesn't can install it manually from
  https://developer.microsoft.com/microsoft-edge/webview2/ if the app
  fails to launch.
- **SmartScreen will likely warn** ("Windows protected your PC") the
  first time someone runs the installer, since it's unsigned. This is
  normal for a personal/indie app without a paid code-signing
  certificate - documented, not something to chase fixing.
- **Port forwarding remains entirely manual** - nothing here automates
  router configuration. See `docs/DESIGN.md` §10 for why.

## Report back

Whatever happens at each step - a clean success, or an error - is
useful information. If something fails, the exact error message (and
which step it happened at) is what actually lets us fix the specific
thing that's wrong, the same way it worked for every `pywebview`-
specific question earlier in this project.
