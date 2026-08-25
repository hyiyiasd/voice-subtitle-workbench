from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "src" / "voice_subtitle_translator" / "gui" / "app.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(root / "models" / "manifest.json"), "models"),
        (str(root / "THIRD_PARTY_NOTICES.md"), "."),
        (str(root / "LICENSE"), "."),
    ],
    hiddenimports=["keyring.backends.Windows", "voice_subtitle_translator.worker"],
)
pyz = PYZ(a.pure)
gui_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="语音转字幕",
    console=False,
)
cli_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vst-cli",
    console=True,
)
coll = COLLECT(
    gui_exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="voice-subtitle-translator",
)
