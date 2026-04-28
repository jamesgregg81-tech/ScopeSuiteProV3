# Field Workflow

## 196B Safe Screen Capture

1. Select the COM port for the OC4USB/IR serial adapter.
2. Leave Legacy Safe ID Mode enabled.
3. Run `Test`.
4. Confirm the profile shows `FLUKE 196B` at `1200 baud`.
5. Use `Screen Capture > Capture Screen`.

The safe 196B path uses `ID` and direct `QP` screen transfer. It must not send `GR` or `PC 9600`.

## Saved Screen Captures

`Screen Capture > Load Saved Capture` previews saved PNG/JPG/BMP files or raw screen-capture BIN files. It does not generate waveform analysis from image pixels.

## Waveform Reports

Use the Waveform tab for QW waveform data, FFT, power-quality calculations, replay export, and deep-memory reconstruction.

## Fluke Connect Inbox

Place Fluke Connect PDFs and measurement CSV exports in:

`C:\FlukeConnect_Inbox`

Then use:

`Waveform > Import Fluke Connect Inbox`

The import package includes copied PDFs, original CSVs, normalized CSV data, and an import summary.
