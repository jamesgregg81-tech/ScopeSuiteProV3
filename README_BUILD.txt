Fluke ScopeMeter Analyzer EXE Build Notes
=========================================

How to build
------------
Run this from the Fluke Scopemeter Python folder:

build_exe.bat

The build script uses PyInstaller in one-file console mode and writes:

dist\FlukeScopeMeterAnalyzer.exe

To build the GUI version, run:

build_gui_exe.bat

That writes:

dist\FlukeScopeMeterAnalyzerGUI.exe


How to run
----------
Run the console version:

dist\FlukeScopeMeterAnalyzer.exe

Keep the console window open so you can see COM-port detection, ACK responses,
ScopeMeter ID output, and any debug/error messages.

Run the GUI version:

dist\FlukeScopeMeterAnalyzerGUI.exe

The GUI has buttons for:

Connect ScopeMeter
Auto Detect COM
Capture Replay
Power Quality Report
Open Reports Folder
Exit


Required Python packages before building
----------------------------------------
Install these into the Python environment you use for the build:

python -m pip install pyinstaller pyserial numpy pandas matplotlib openpyxl

The project requirements.txt already lists the runtime packages:

pyserial
numpy
pandas
matplotlib
openpyxl

PyInstaller is also required to create the EXE.


Report output
-------------
The EXE preserves the script's existing report output behavior. Reports are
still saved under:

C:\Users\JimGr\Desktop\FlukeReplayFinalReports

The EXE keeps the console visible and prints:

Fluke ScopeMeter Analyzer EXE Starting...

at startup.
