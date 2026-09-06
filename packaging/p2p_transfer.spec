# p2p_transfer.spec
#
# PyInstaller spec file for building the Windows distribution.
# Run from the PROJECT ROOT (not from inside packaging/) with:
#     pyinstaller packaging/p2p_transfer.spec
#
# All paths below are written relative to the project root, matching
# that invocation - if the build fails immediately with a "file not
# found" error for gui_app.py, that's the first thing to check: some
# PyInstaller versions resolve spec-file-relative paths relative to
# the spec file's own folder instead of the CWD. Can't verify which
# behavior applies without actually running PyInstaller on Windows,
# which isn't possible from this sandbox at all.
#
# Deliberately --onedir (a folder), NOT --onefile (a single packed
# exe) - see docs/DESIGN.md / docs/HISTORY.md for why: single-file
# packed executables are meaningfully more likely to trigger antivirus
# false-positive flags, purely from looking suspicious to heuristic
# scanners, than a plain folder of files does. The Analysis -> PYZ ->
# EXE(exclude_binaries=True) -> COLLECT structure below IS the
# standard --onedir pattern.
#
# Scope note: this bundles gui_app.py (the GUI) only, not cli.py - the
# distributed installer's target audience is explicitly "a normal user
# with no interest in a terminal" (see DESIGN.md §1); cli.py remains a
# run-from-source tool for anyone comfortable with that.
#
# NOTE ON hiddenimports BELOW: pywebview dynamically selects a backend
# at runtime based on what's actually available on the system.
# PyInstaller's static import analysis can miss these platform-
# specific submodules, since they're never directly `import`-ed
# anywhere in our own source. If the built .exe fails to start with a
# "no module named webview.platforms.X" error, that confirms this
# list needs adjusting - this genuinely can't be verified from a
# Linux sandbox, since building/running a Windows .exe isn't possible
# here at all.

block_cipher = None

a = Analysis(
    ['src/gui_app.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('src/gui', 'gui'),  # bundles the whole gui/ folder (index.html) alongside the exe
    ],
    hiddenimports=[
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='P2PTransfer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=False hides the black terminal window that would
    # otherwise appear behind the GUI window - a raw Python script
    # doesn't need one, and having one appear would look unfinished
    # for a real distributed app.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='P2PTransfer',
)
