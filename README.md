# ScopeSuite Pro V3 Industrial Edition

ScopeSuite Pro V3 is a Windows field application for Fluke ScopeMeter and Fluke Connect data, built for generator diagnostics, load-bank testing, power-quality review, and practical reporting.

It bridges:

- High-speed waveform capture from Fluke ScopeMeter 19x-family instruments.
- Mid/low-speed trend and measurement exports from Fluke Connect tools.
- Field-ready reports for technicians and customers.

## Key Capabilities

- Safe legacy serial workflow for Fluke 196B/199C at 1200 baud.
- Screen capture preview with 196B monochrome-to-color rendering for readability.
- Waveform capture, FFT, harmonic, power, and report generation.
- Replay/deep-memory reconstruction from replay frame exports.
- Fluke Connect inbox import from `C:\FlukeConnect_Inbox`.
- Universal Fluke CSV normalization for exported measurement files.
- Tablet/sunlight UI modes and regression smoke tests.

## Instrument Integration

- Fluke 196B / 199C ScopeMeter serial communication.
- Fluke 378 FC Fluke Connect CSV import.
- Fluke 289-style logging data support through CSV import.

## Data Ingestion

The Fluke Connect import workflow scans:

```text
C:\FlukeConnect_Inbox
```

Supported files:

- CSV measurement exports.
- PDF reference reports.
- Field documentation images where applicable.

The parser handles BOM/encoding issues, common Fluke header variants, mixed measurement units, and L1/L2/L3 snapshot rows.

## Waveform And Reports

ScopeSuite can produce:

- Screen capture PNGs.
- Raw capture files.
- Decoded waveform CSVs.
- Waveform and FFT plots.
- Harmonic summaries.
- Professional HTML/PDF reports.
- Deep-memory reconstructed waveform/trend outputs.

## Run From Source

```powershell
python FlukeScopeSuite_Pro_v3.py
```

## Smoke Test

```powershell
python FlukeScopeSuite_Pro_v3.py --field-abuse-self-test --self-test-log C:\Users\JimGr\Desktop\scopesuite_smoke.log
python tests\run_smoke_tests.py --self-test-log C:\Users\JimGr\Desktop\scopesuite_tests_wrapper.log
```

## Build

```powershell
.\build_scopesuite_v2_autotune.bat
```

Generated EXE files are intentionally ignored by Git.

## Source Layout

```text
ScopeSuiteProV3/
  scopesuite_v3/
  tests/
  sample_data/
  docs/
  requirements.txt
  README.md
  README_BUILD.txt
  *.spec
```

## Screenshots

Screenshots live under `docs/screenshots/`.

## Known Limitations

Fluke Connect CSV exports may not include phase rotation, true power factor, harmonics, or THD. Those values must come from ScopeMeter waveform data, supplemental instruments, or manual notes.

## Intended Use

- Generator load-bank testing.
- ATS transfer analysis.
- Power-quality troubleshooting.
- Field diagnostics and reporting.
