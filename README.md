# ScopeSuite Pro V3 – Industrial Edition

**ScopeSuite Pro V3** is a Windows-based analysis tool designed for **Fluke ScopeMeter and Fluke Connect data**, built specifically for **generator diagnostics, load testing, and power system analysis**.

It bridges the gap between:

* High-speed waveform capture (ScopeMeter)
* Mid/low-speed trending (Fluke Connect / Fluke 289)
* Real-world field diagnostics

---

## 🚀 Key Capabilities

* Import Fluke Connect CSV logs and reports
* Replay ScopeMeter waveform captures
* Detect load events (drops, spikes, transfers)
* Generate professional, field-ready reports
* Combine multiple data sources into one analysis

---

## 📊 Feature List

### 🔌 Instrument Integration

* Fluke 199C / 196B ScopeMeter (serial communication)
* Fluke 378 FC (Fluke Connect CSV import)
* Fluke 289 (logging data support)

---

### 📥 Data Ingestion

* Automatic import from:

  ```
  C:\FlukeConnect_Inbox
  ```

* Supports:

  * CSV (primary analysis data)
  * PDF (reference reports)
  * Images (field documentation)

* Handles:

  * Single-channel logs (current, voltage, temp)
  * Dual-channel logs (amps + volts)
  * Snapshot measurement tables (L1/L2/L3)

---

### 📈 Waveform & Trend Analysis

* ScopeMeter waveform replay
* Time-series graphing
* Dual-axis plotting (V + A)
* Automatic scaling for field/tablet visibility

---

### ⚡ Event Detection Engine

Automatically detects and flags:

* Voltage dropouts
* Load spikes / inrush events
* Transfer events (ATS behavior)
* Voltage sag and recovery
* Abnormal zero readings
* Outlier / corrupted values
* Phase imbalance (3-phase systems)

---

### 🔧 Data Conditioning

* Handles messy Fluke CSV exports:

  * Duplicate timestamps
  * Missing headers
  * Mixed data blocks
  * Encoding/BOM issues

* Filters invalid readings:

  * Unrealistic spikes
  * Range-switch artifacts
  * Sensor dropouts

---

### 🧠 Multi-Rate Data Support

Combines:

| Source        | Type                | Purpose              |
| ------------- | ------------------- | -------------------- |
| ScopeMeter    | High-speed          | Transients, waveform |
| Fluke Connect | Mid-speed (~5 Hz)   | Event detection      |
| Fluke 289     | Low-speed (~1–4 Hz) | Long-term trends     |

---

### 📄 Reporting Engine

Generates professional reports with:

* Asset summary
* Test point breakdown
* Graph-first layout (not raw data dumps)
* Event summary
* Min / Max / Average values
* Raw data appendix
* Attached field photos
* Embedded Fluke reference PDFs

---

### 🖥️ GUI (Field-Optimized)

* Large touch-friendly controls
* Dark mode support
* Real-time status display:

  * Connected
  * Transferring
  * Importing
  * Analyzing
* Optimized for tablets and laptops

---

## 📁 Project Structure

```
ScopeSuiteProV3/
  README.md
  requirements.txt
  scopesuite_v3/
  tests/
  sample_data/
  docs/
  build/
  dist/
```

---

## 📸 Screenshots

> Place images in: `docs/screenshots/`

```
docs/screenshots/
  main_ui.png
  waveform.png
  trend.png
  report.png
```

Example:

```
![Main UI](docs/screenshots/main_ui.png)
![Waveform](docs/screenshots/waveform.png)
![Trend](docs/screenshots/trend.png)
![Report](docs/screenshots/report.png)
```

---

## 🛠️ Build Instructions

Install dependencies:

```
pip install -r requirements.txt
```

Build EXE:

```
pyinstaller FlukeScopeMeterAnalyzerGUI.spec
```

---

## 🧪 Testing

Run smoke tests:

```
python -m tests.run_tests
```

---

## ⚠️ Known Limitations

Fluke Connect CSV exports do **not include**:

* Phase rotation
* True power factor values
* Harmonics / THD

These must be:

* inferred
* manually entered
* or supplemented with ScopeMeter data

---

## 🔧 Roadmap

* Phase angle estimation
* True 3-phase power calculations
* Live data streaming
* Advanced PQ metrics (THD, PF reconstruction)
* Automated load bank test reports

---

## 📌 Intended Use

* Generator load bank testing
* ATS transfer analysis
* Power quality troubleshooting
* Field diagnostics and reporting

---

## 📄 License

Internal / proprietary (adjust as needed)
