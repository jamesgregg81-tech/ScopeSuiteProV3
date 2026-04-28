ScopeSuite Pro V3 Build Notes
=============================

1. Install Python 3.12.
2. Install dependencies:

   python -m pip install -r requirements.txt pyinstaller

3. Build the Industrial Edition EXE:

   build_scopesuite_v2_autotune.bat

4. Run source smoke tests:

   python FlukeScopeSuite_Pro_v3.py --field-abuse-self-test --self-test-log C:\Users\JimGr\Desktop\scopesuite_source_smoke.log

5. Run packaged smoke tests:

   Start-Process .\dist\FlukeScopeSuiteV2AutoTune.exe -ArgumentList @('--field-abuse-self-test','--self-test-log','C:\Users\JimGr\Desktop\scopesuite_exe_smoke.log') -PassThru -WindowStyle Hidden

Do not commit dist/, build/, EXE files, caches, or log files.
