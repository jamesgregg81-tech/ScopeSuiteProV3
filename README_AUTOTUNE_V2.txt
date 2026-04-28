FlukeScopeSuite V2 AutoTune / Generator Tuning
==============================================

Purpose
-------
Offline Windows-tablet Generator AutoTune Assistant for Fluke 199C waveform captures.

Safety
------
AutoTune provides recommendations only. Generator controller adjustments must be performed by qualified personnel.

AutoTune never writes settings to a generator controller. It records technician-entered before/final values only after confirmation.

Tablet Workflow
---------------
1. Connect Scope
2. Capture No-Load Baseline
3. Capture Load-Step Event
4. Analyze Response
5. Show Recommendation
6. Save Final Settings
7. Generate Commissioning Report

Output Package
--------------
Each AutoTune session writes a local folder under:

Desktop\FlukeScopeSuite_Captures\reports\autotune_YYYY-MM-DD_HH-MM-SS

Generated files include:

AUTOTUNE_REPORT.html
AUTOTUNE_REPORT.pdf when ReportLab is installed
baseline_waveform.csv
load_step_waveform.csv
voltage_frequency_recovery.png
fft_harmonic_plot.png
before_after_gov_reg_settings.json
autotune_analysis.json

Build
-----
From this folder, run:

build_scopesuite_v2_autotune.bat

The build script prefers Python 3.12 via py -3.12 when installed, and falls back to python.

Runtime Dependencies
--------------------
Required:
pyserial
numpy
pillow

Preferred:
matplotlib

Optional:
reportlab, for PDF output

No cloud/API dependency is used.
