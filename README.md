# ScopeSuite Pro V3 Industrial Edition

Field-grade PC workflow for Fluke ScopeMeter 19x-family instruments, including the Fluke 196B and 199C.

## Highlights

- Safe legacy serial workflow for Fluke 196B/199C at 1200 baud.
- Screen capture preview with 196B monochrome-to-color rendering for readability.
- Waveform capture, FFT, harmonic, power, and report generation.
- Replay/deep-memory reconstruction from replay frame exports.
- Fluke Connect inbox import from `C:\FlukeConnect_Inbox`.
- Universal Fluke CSV normalization for exported measurement files.
- Tablet/sunlight UI modes and regression smoke tests.

## Run From Source

```powershell
python FlukeScopeSuite_Pro_v3.py
```

## Smoke Test

```powershell
python FlukeScopeSuite_Pro_v3.py --field-abuse-self-test --self-test-log C:\Users\JimGr\Desktop\scopesuite_smoke.log
```

## Build

```powershell
.\build_scopesuite_v2_autotune.bat
```

The generated EXE is intentionally ignored by Git.

## Source Layout

- `scopesuite_v3/` - application source.
- `tests/` - command-line smoke test wrapper.
- `sample_data/` - small sample import files.
- `docs/` - field workflow notes.
- `FlukeScopeSuiteV2AutoTune.spec` - PyInstaller spec.
- `build_scopesuite_v2_autotune.bat` - Windows build script.
