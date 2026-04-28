import csv
import math
import re
import tempfile
import time
import traceback
from pathlib import Path

from .screen_capture_modes import screen_capture_filename_prefix, screen_capture_mode_for_ident


def run_screen_capture_model_tests():
    cases = [
        ("FLUKE 196B;V06.12;ENGLISH", "legacy", False, "fluke196b_screen"),
        ("FLUKE 199C;V06.13;2003-11-25;ENGLISH", "png", True, "fluke199c_screen"),
    ]
    results = []
    for ident, expected_mode, expected_png, expected_prefix in cases:
        mode, png_supported, model = screen_capture_mode_for_ident(ident)
        prefix = screen_capture_filename_prefix(ident)
        passed = mode == expected_mode and png_supported is expected_png and prefix == expected_prefix
        results.append({
            "ident": ident,
            "model": model,
            "mode": mode,
            "png_supported": png_supported,
            "prefix": prefix,
            "passed": passed,
            "expected": {
                "mode": expected_mode,
                "png_supported": expected_png,
                "prefix": expected_prefix,
            },
        })
    return results


def run_live_single_report_smoke(argv=None):
    argv = argv or []
    log_path = Path.cwd() / f"live_single_report_smoke_{time.strftime('%Y%m%d_%H%M%S')}.log"
    if "--self-test-log" in argv:
        idx = argv.index("--self-test-log")
        if idx + 1 < len(argv):
            log_path = Path(argv[idx + 1])
    live_port = "COM10"
    if "--port" in argv:
        idx = argv.index("--port")
        if idx + 1 < len(argv):
            live_port = argv[idx + 1]

    lines = []

    def write(line):
        lines.append(str(line))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    try:
        import tkinter as tk
        from PIL import Image
        from .app import FlukeScopeSuiteProV3

        root = tk.Tk()
        root.withdraw()
        app = FlukeScopeSuiteProV3(root)
        app.ui_call = lambda func, *args, **kwargs: func(*args, **kwargs)
        app.show_error = lambda title, error: write(f"DIALOG {title}: {error}")
        app.log_msg = write
        app.port_var.set(live_port)
        app.safe_199c_mode_var.set(True)
        app.advanced_transfer_mode_var.set(False)

        write(f"START live single waveform smoke on {live_port}")
        app.test_connection()
        require(app.instrument_id.upper().startswith("FLUKE"), f"ID failed: {app.instrument_id}")
        require(app.instrument_profile.get("baud") == 1200, f"Profile baud not 1200: {app.instrument_profile}")
        require(app.instrument_profile.get("safe_mode") is True, f"Profile not safe: {app.instrument_profile}")
        require("NOT_CONNECTED" not in str(app.outdir).upper(), f"Output folder not renamed after connect: {app.outdir}")
        write(f"PASS connect profile {app.active_profile_log_text()}")

        app.single_waveform_report()
        report_dir = Path(app.latest_report_dir)
        require(report_dir.exists(), f"Report dir missing: {report_dir}")
        require("NOT_CONNECTED" not in str(report_dir).upper(), f"Report folder still NOT_CONNECTED: {report_dir}")
        expected = [
            "SINGLE_WAVEFORM_REPORT.txt",
            "PROFESSIONAL_REPORT.html",
            "SUMMARY_METRICS.csv",
            "waveform_samples.csv",
            "waveform_plot.png",
            "fft_spectrum.png",
            "harmonic_summary.png",
        ]
        created = []
        for name in expected:
            path = report_dir / name
            require(path.exists() and path.stat().st_size > 0, f"Missing/empty report file: {name}")
            created.append(name)
        html = (report_dir / "PROFESSIONAL_REPORT.html").read_text(encoding="utf-8", errors="replace").lower()
        require("<html" in html and "</html>" in html, "Professional HTML missing valid structure")
        with (report_dir / "SUMMARY_METRICS.csv").open("r", encoding="utf-8", errors="replace", newline="") as f:
            row = next(csv.DictReader(f))
        for key in ("scope_id", "vrms_v", "irms_a", "frequency_hz"):
            raw = str(row.get(key, "")).strip()
            require(raw and raw.lower() not in ("nan", "none", "null"), f"Bad SUMMARY_METRICS field {key}={raw!r}")
        with Image.open(report_dir / "waveform_plot.png") as img:
            img.verify()
        log_text = "\n".join(lines)
        require("TX: GR" not in log_text, "Live safe report issued GR")
        require("TX: PC" not in log_text, "Live safe report issued PC baud change")
        require("TX: QW 10" in log_text, "Live safe report did not issue QW 10")
        require("TX: QW 20" in log_text, "Live safe report did not issue QW 20")
        write(f"PASS live single waveform report files: {', '.join(created)}")
        write("RESULT PASS failures=0")
        root.destroy()
        return 0
    except Exception as exc:
        write(f"RESULT FAIL: {type(exc).__name__}: {exc}")
        write(traceback.format_exc())
        try:
            root.destroy()
        except Exception:
            pass
        return 1


def assert_screen_capture_model_tests():
    results = run_screen_capture_model_tests()
    failures = [result for result in results if not result["passed"]]
    if failures:
        raise AssertionError(f"Screen capture model tests failed: {failures}")
    return results


def run_field_abuse_selftest(argv=None):
    argv = argv or []
    log_path = Path.cwd() / f"field_abuse_selftest_{time.strftime('%Y%m%d_%H%M%S')}.log"
    if "--self-test-log" in argv:
        idx = argv.index("--self-test-log")
        if idx + 1 < len(argv):
            log_path = Path(argv[idx + 1])
    live_port = None
    if "--port" in argv:
        idx = argv.index("--port")
        if idx + 1 < len(argv):
            live_port = argv[idx + 1]

    lines = []

    def write(line):
        lines.append(line)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def check(name, func):
        try:
            func()
            write(f"PASS {name}")
            return True
        except Exception as exc:
            write(f"FAIL {name}: {type(exc).__name__}: {exc}")
            write(traceback.format_exc())
            return False

    try:
        import tkinter as tk
        from .app import FlukeScopeSuiteProV3
        from .professional_report import build_professional_report_package
        from .waveform_protocol import analyze_deep_memory_capture
    except Exception as exc:
        write(f"FAIL import: {exc}")
        return 1

    root = tk.Tk()
    root.withdraw()
    app = FlukeScopeSuiteProV3(root)
    app.ui_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    app.show_error = lambda title, error: app.log_msg(f"DIALOG {title}: {error}")

    def iter_widgets(widget):
        yield widget
        for child in widget.winfo_children():
            yield from iter_widgets(child)

    def visible_button_texts():
        texts = []
        for widget in iter_widgets(root):
            try:
                if widget.winfo_class() in ("TButton", "Button"):
                    text = str(widget.cget("text")).strip()
                    if text:
                        texts.append(text)
            except Exception:
                pass
        return texts

    def validate_text_file(path, required=()):
        path = Path(path)
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"Missing or empty text file: {path.name}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if "Traceback" in text:
            raise RuntimeError(f"Traceback text found in {path.name}")
        for token in required:
            if token not in text:
                raise RuntimeError(f"{path.name} missing required text: {token}")
        return text

    def validate_csv_file(path, required_headers=(), critical_fields=()):
        path = Path(path)
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"Missing or empty CSV file: {path.name}")
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
        missing = [h for h in required_headers if h not in headers]
        if missing:
            raise RuntimeError(f"{path.name} missing CSV headers: {missing}")
        if not rows:
            raise RuntimeError(f"{path.name} has no data rows")
        for field in critical_fields:
            raw = str(rows[0].get(field, "")).strip()
            if not raw:
                raise RuntimeError(f"{path.name} blank critical field: {field}")
            if raw.lower() in ("nan", "none", "null"):
                raise RuntimeError(f"{path.name} invalid critical field {field}: {raw}")
        return headers, rows

    def validate_html_file(path):
        text = validate_text_file(path)
        lowered = text.lower()
        if "<html" not in lowered or "</html>" not in lowered:
            raise RuntimeError(f"{Path(path).name} is not valid-looking HTML")
        return text

    def validate_image_file(path):
        from PIL import Image
        path = Path(path)
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(f"Missing or empty image file: {path.name}")
        with Image.open(path) as img:
            img.verify()
        return True

    def validate_single_waveform_report(report_dir, ident_token="FLUKE 196B"):
        report_dir = Path(report_dir)
        expected = [
            "SINGLE_WAVEFORM_REPORT.txt",
            "PROFESSIONAL_REPORT.html",
            "SUMMARY_METRICS.csv",
            "waveform_samples.csv",
            "waveform_plot.png",
            "fft_spectrum.png",
            "harmonic_summary.png",
        ]
        created = []
        for name in expected:
            path = report_dir / name
            if not path.exists() or path.stat().st_size <= 0:
                raise RuntimeError(f"Expected report file missing/empty: {name}")
            created.append(name)
        validate_text_file(
            report_dir / "SINGLE_WAVEFORM_REPORT.txt",
            required=(ident_token, "Timestamp", "Primary source: QW numeric waveform data"),
        )
        validate_html_file(report_dir / "PROFESSIONAL_REPORT.html")
        validate_csv_file(
            report_dir / "SUMMARY_METRICS.csv",
            required_headers=("timestamp", "scope_id", "vrms_v", "irms_a", "frequency_hz", "power_factor"),
            critical_fields=("scope_id", "vrms_v", "irms_a", "frequency_hz"),
        )
        validate_csv_file(
            report_dir / "waveform_samples.csv",
            required_headers=("time_s", "channel_a_v", "channel_b_a"),
        )
        for name in ("waveform_plot.png", "fft_spectrum.png", "harmonic_summary.png"):
            validate_image_file(report_dir / name)
        write(f"FILES single waveform report: {', '.join(created)}")
        return created

    failures = 0
    for geom in ("720x500", "853x533", "1024x640", f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}"):
        def layout_pass(geom=geom):
            root.geometry(geom)
            root.update_idletasks()
            for scale in ("Compact", "Standard", "Tablet"):
                app.ui_scale_var.set(scale)
                for mode in ("Light", "Dark", "Sunlight"):
                    app.field_mode_var.set(mode)
                    app.apply_tablet_style()
                    root.update_idletasks()
        failures += 0 if check(f"layout/theme {geom}", layout_pass) else 1

    def visible_button_inventory_check():
        texts = visible_button_texts()
        required = [
            "Refresh", "Test", "Capture Screen", "Image-Only Screen Report", "Live Waveform Analysis",
            "Load Saved Capture", "Single Waveform Report", "Export Replay Set",
            "Analyze Full Capture (Deep Memory)", "Load Raw Waveform", "Import Fluke Connect Inbox",
            "Compute FFT from Loaded Waveform",
            "Copy Summary", "Open Reports Folder", "Generate Commissioning Report",
            "Refresh Settings Display", "Open Output Folder", "Clear Log",
        ]
        missing = [text for text in required if text not in texts]
        if missing:
            raise RuntimeError(f"Visible button inventory missing: {missing}; found={texts}")
        write(f"BUTTONS visible/inventoried: {len(texts)}")

    failures += 0 if check("visible GUI button inventory", visible_button_inventory_check) else 1

    failures += 0 if check("no COM selected", lambda: (app.port_var.set(""), app.test_connection())) else 1
    failures += 0 if check("wrong COM port", lambda: (app.port_var.set("COM999"), app.test_connection())) else 1

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def open_reports_folder_check():
            calls = []
            old_open = app.open_path_external
            try:
                app.open_path_external = lambda path: calls.append(Path(path))
                app.open_folder()
                if not calls or calls[-1] != Path(app.outdir):
                    raise RuntimeError(f"Open Output Folder did not target outdir: {calls}")
            finally:
                app.open_path_external = old_open

        failures += 0 if check("open output/reports folder", open_reports_folder_check) else 1

        bad_capture = tmp / "bad_capture.bin"
        bad_capture.write_bytes(b"not an escp or png capture")
        failures += 0 if check("corrupted capture replay", lambda: app.replay_capture(bad_capture)) else 1

        def saved_screen_load_stays_in_capture_tab():
            from PIL import Image

            png = tmp / "saved_screen.png"
            Image.new("RGB", (320, 240), (255, 255, 255)).save(png)
            old_report = app.latest_report_dir
            app.tabs.select(app.reports_tab)
            app.load_saved_capture(png)
            root.update()
            root.update_idletasks()
            if app.tabs.select() != str(app.capture_tab):
                raise RuntimeError("Loading a saved screen capture should leave the user in Screen Capture.")
            if app.latest_report_dir != old_report:
                raise RuntimeError("Loading a saved screen capture should not generate or select an analyzer report.")
            loaded_reports = list(Path(app.outdir).glob("reports/loaded_capture_*"))
            if loaded_reports:
                raise RuntimeError(f"Saved screen preview generated analysis reports unexpectedly: {loaded_reports}")

        failures += 0 if check("saved screen load stays in capture tab", saved_screen_load_stays_in_capture_tab) else 1

        tiny_wave = tmp / "tiny_waveform.bin"
        tiny_wave.write_bytes(b"123")
        failures += 0 if check("tiny corrupted waveform load", lambda: app.load_waveform(tiny_wave)) else 1

        def fluke_connect_import_check():
            from .fluke_connect import parse_fluke_csv

            inbox = tmp / "fluke_connect_inbox"
            inbox.mkdir()
            source_csv = inbox / "Measurements.csv"
            source_csv.write_text(
                "Model Number,Tool Name,Measurement  Date,Configuration,Measurement,Unit,Additional Information,Note,Asset,Work Order\n"
                "FLUKE F378FC,AMP CLAMP 378FC,2026-03-25 11:11:12,L1-N,11.6,Aac,PQ-Amps,,Site A,WO-1\n"
                "FLUKE F378FC,AMP CLAMP 378FC,2026-03-25 11:11:12,L1-N,264,Vac,PQ-Amps,,Site A,WO-1\n",
                encoding="utf-8",
            )
            (inbox / "FlukeConnectReport.pdf").write_bytes(b"%PDF-1.4\n% synthetic\n")
            rows = parse_fluke_csv(source_csv)
            if len(rows) != 2 or rows[0]["model"] != "FLUKE F378FC" or rows[1]["unit"] != "Vac":
                raise RuntimeError(f"Universal Fluke CSV parser returned bad rows: {rows}")
            old_inbox = app.fluke_connect_inbox_var.get()
            old_report = app.latest_report_dir
            try:
                app.fluke_connect_inbox_var.set(str(inbox))
                app.import_fluke_connect_inbox()
                report_dir = Path(app.latest_report_dir)
                if report_dir == old_report or not report_dir.exists():
                    raise RuntimeError("Fluke Connect import did not register a new report folder.")
                validate_text_file(report_dir / "FLUKE_CONNECT_IMPORT_SUMMARY.txt", ["Measurement rows normalized: 2"])
                validate_csv_file(
                    report_dir / "fluke_connect_measurements_normalized.csv",
                    required_headers=["model", "measurement_datetime", "measurement", "unit", "asset"],
                    critical_fields=["model", "measurement", "unit"],
                )
                if not (report_dir / "FlukeConnectReport.pdf").exists():
                    raise RuntimeError("Fluke Connect PDF was not copied into the import package.")
            finally:
                app.fluke_connect_inbox_var.set(old_inbox)

        failures += 0 if check("Fluke Connect import and universal CSV parser", fluke_connect_import_check) else 1

        report_dir = tmp / "extreme_report"
        report_dir.mkdir()
        with (report_dir / "replay_p000_waveforms.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "channel_a_voltage", "channel_b_current"])
            writer.writeheader()
            for i in range(80):
                writer.writerow({
                    "time": i / 1000,
                    "channel_a_voltage": ((-1) ** i) * 1e308,
                    "channel_b_current": ((-1) ** (i + 1)) * 9000,
                })
        failures += 0 if check(
            "professional report extreme values",
            lambda: build_professional_report_package(report_dir, "FLUKE 196B TEST", "Abuse Report", log=app.log_msg),
        ) else 1
        failures += 0 if check("register/refresh extreme report", lambda: app.register_report_session(report_dir, announce=True)) else 1
        failures += 0 if check("copy executive summary", app.copy_summary_to_clipboard) else 1

        app.latest_report_dir = tmp / "missing_report"
        failures += 0 if check("missing report folder refresh", app.refresh_reports_tab) else 1

        def simulated_safe_connect_profile_check():
            old_outdir = app.outdir
            old_session_root = app.session_root
            old_session_dir = app.session_dir
            old_port = app.port_var.get()
            old_safe = app.safe_199c_mode_var.get()
            old_serial_client = app.serial_client
            old_profile = dict(app.instrument_profile)
            commands = []

            class FakeSer:
                is_open = True
                timeout = 1.0

                def flush(self):
                    pass

                def reset_input_buffer(self):
                    pass

                def reset_output_buffer(self):
                    pass

                def close(self):
                    self.is_open = False

                def read(self, _n=1):
                    return b""

            class FakeClient:
                baudrate = 1200

                def __init__(self, ident):
                    self.ident = ident

                def open(self):
                    return FakeSer()

                def query_ascii_allow_ack(self, ser, cmd, timeout=8.0, clear_input=True):
                    commands.append(cmd)
                    if cmd != "ID":
                        raise RuntimeError(f"Unexpected command: {cmd}")
                    return b"0", self.ident

            def run_case(ident, expected_model):
                commands.clear()
                root_dir = tmp / f"profile_{expected_model}"
                root_dir.mkdir()
                app.session_root = root_dir
                app.session_dir = root_dir / "2026-04-27_NOT_CONNECTED"
                app.session_dir.mkdir()
                app.outdir = app.session_dir
                app.port_var.set("COM10")
                app.safe_199c_mode_var.set(True)
                app.serial_client = lambda baudrate=1200, xonxoff=False, ident=ident: FakeClient(ident)
                app.test_connection()
                if commands != ["ID"]:
                    raise RuntimeError(f"{expected_model}: expected ID only, got {commands}")
                if app.instrument_profile.get("model") != expected_model:
                    raise RuntimeError(f"{expected_model}: profile model mismatch {app.instrument_profile}")
                if app.instrument_profile.get("baud") != 1200 or app.instrument_profile.get("port") != "COM10":
                    raise RuntimeError(f"{expected_model}: profile port/baud mismatch {app.instrument_profile}")
                if "NOT_CONNECTED" in str(app.outdir).upper():
                    raise RuntimeError(f"{expected_model}: output folder was not renamed after ID: {app.outdir}")
                write(f"PROFILE {expected_model}: {app.active_profile_log_text()}")

            try:
                run_case("FLUKE 196B;V06.12;2003-06-17", "196B")
                run_case("FLUKE 199C;V06.13;2003-11-25", "199C")
            finally:
                app.outdir = old_outdir
                app.session_root = old_session_root
                app.session_dir = old_session_dir
                app.port_var.set(old_port)
                app.safe_199c_mode_var.set(old_safe)
                app.serial_client = old_serial_client
                app.instrument_profile = old_profile

        failures += 0 if check("simulated 196B/199C safe connect profiles", simulated_safe_connect_profile_check) else 1

        fallback_png = tmp / "fallback_screen.png"
        try:
            from PIL import Image
            Image.new("RGB", (320, 240), (245, 245, 245)).save(fallback_png)
        except Exception as exc:
            raise RuntimeError(f"Could not create fallback PNG fixture: {exc}") from exc

        def image_only_fallback_check():
            old_outdir = app.outdir
            old_last_png = app.last_png
            old_safe = app.safe_199c_mode_var.get()
            old_state = app.connection_state_var.get()
            try:
                app.outdir = tmp / "fallback_output"
                app.outdir.mkdir(parents=True, exist_ok=True)
                app.last_png = fallback_png
                app.safe_199c_mode_var.set(True)
                app.connection_state_var.set("DISCONNECTED")
                app.analyze_this_screen()
                report_dir = Path(app.latest_report_dir)
                report = report_dir / "ANALYZE_THIS_SCREEN.txt"
                summary = report_dir / "SUMMARY_METRICS.csv"
                screen = report_dir / "screen_capture.png"
                for path in (report, summary, screen):
                    if not path.exists() or path.stat().st_size <= 0:
                        raise RuntimeError(f"Missing fallback output: {path.name}")
                text = report.read_text(encoding="utf-8")
                warning = (
                    "NO LIVE SCOPEMETER DATA USED. No GR, no QW, no serial capture, "
                    "no real waveform samples. Metrics are limited to screenshot/image-mode status only."
                )
                if "Analyze This Screen - Image Only Fallback Report" not in text:
                    raise RuntimeError("Fallback report title is not explicit.")
                if warning not in text:
                    raise RuntimeError("Fallback report warning is missing.")
                if (report_dir / "waveform_samples.csv").exists():
                    raise RuntimeError("Fallback should not create waveform_samples.csv.")
                with summary.open("r", encoding="utf-8", newline="") as f:
                    row = next(csv.DictReader(f))
                expected = {
                    "analysis_mode": "PNG_FALLBACK_ONLY",
                    "connected": "false",
                    "live_serial_used": "false",
                    "waveform_samples_valid": "false",
                    "power_quality_valid": "false",
                }
                for key, value in expected.items():
                    if row.get(key) != value:
                        raise RuntimeError(f"Fallback summary {key}={row.get(key)!r}, expected {value!r}")
                labels = [app.report_listbox.get(i) for i in range(app.report_listbox.size())]
                for name in ("screen_capture.png", "ANALYZE_THIS_SCREEN.txt", "SUMMARY_METRICS.csv"):
                    if name not in labels:
                        raise RuntimeError(f"Fallback UI file list missing exact label: {name}")
                if app.report_package_title_var.get() != "Image-only fallback report completed.":
                    raise RuntimeError("Fallback UI completion label is not explicit.")
                app.preview_report_file(screen)
                if app.report_preview_title_var.get().startswith("Preview Error"):
                    raise RuntimeError(app.report_text.get("1.0", "end").strip())
            finally:
                app.outdir = old_outdir
                app.last_png = old_last_png
                app.safe_199c_mode_var.set(old_safe)
                app.connection_state_var.set(old_state)

        failures += 0 if check("not connected image-only fallback report", image_only_fallback_check) else 1

        def safe_mode_single_waveform_unavailable_check():
            from . import waveform_protocol

            old_outdir = app.outdir
            old_port = app.port_var.get()
            old_safe = app.safe_199c_mode_var.get()
            old_advanced = app.advanced_transfer_mode_var.get()
            old_serial_client = app.serial_client
            old_query_waveform = waveform_protocol.query_waveform
            old_profile = dict(app.instrument_profile)
            old_ident = app.instrument_id
            old_baud = app.current_scope_baud
            commands = []

            class FakeSer:
                is_open = True

                def flush(self):
                    pass

                def reset_input_buffer(self):
                    pass

                def reset_output_buffer(self):
                    pass

                def close(self):
                    self.is_open = False

            class FakeClient:
                baudrate = 1200

                def open(self):
                    return FakeSer()

                def query_ascii_allow_ack(self, ser, cmd, timeout=8.0, clear_input=True):
                    commands.append(cmd)
                    if cmd != "ID":
                        raise RuntimeError(f"Unexpected safe-mode command: {cmd}")
                    return b"0", "FLUKE 196B;V06.12;2003-06-17"

                def send_cmd(self, ser, cmd, *args, **kwargs):
                    commands.append(cmd)
                    if cmd in ("GR", "PC 9600"):
                        raise RuntimeError(f"Forbidden safe-mode command: {cmd}")

            def fake_serial_client(baudrate=1200, xonxoff=False):
                if baudrate != 1200:
                    raise RuntimeError(f"Safe mode should not open baud {baudrate}")
                return FakeClient()

            def fake_query_waveform(ser, client, trace_no):
                commands.append(f"QW {trace_no}")
                raise TimeoutError("synthetic QW timeout")

            try:
                app.outdir = tmp / "safe_single_output"
                app.outdir.mkdir(parents=True, exist_ok=True)
                app.port_var.set("COM10")
                app.safe_199c_mode_var.set(True)
                app.advanced_transfer_mode_var.set(False)
                app.instrument_id = "FLUKE 196B;V06.12;2003-06-17"
                app.current_scope_baud = 1200
                app.update_instrument_profile(
                    ident=app.instrument_id,
                    port="COM10",
                    baud=1200,
                    safe_mode=True,
                    remote_used=False,
                )
                app.serial_client = fake_serial_client
                waveform_protocol.query_waveform = fake_query_waveform
                app.single_waveform_report()

                report_dir = Path(app.latest_report_dir)
                report = report_dir / "WAVEFORM_REPORT_UNAVAILABLE.txt"
                if not report.exists() or report.stat().st_size <= 0:
                    raise RuntimeError("Missing safe-mode unavailable diagnostic report.")
                files = [p.name for p in report_dir.iterdir() if p.is_file()]
                if files != ["WAVEFORM_REPORT_UNAVAILABLE.txt"]:
                    raise RuntimeError(f"Safe-mode unavailable report should create one diagnostic TXT, got {files}")
                text = report.read_text(encoding="utf-8")
                if "Waveform data unavailable in Legacy Safe ID Mode. Use Replay Capture or Image-Only Screen Snapshot." not in text:
                    raise RuntimeError("Safe-mode unavailable message missing.")
                if "Model: FLUKE 196B" not in text:
                    raise RuntimeError("196B model was not preserved in diagnostic report.")
                if any(cmd.startswith("GR") or cmd.startswith("PC") for cmd in commands):
                    raise RuntimeError(f"Forbidden safe-mode command issued: {commands}")
                if commands != ["ID", "QW 10", "QW 20"]:
                    raise RuntimeError(f"Unexpected safe-mode command sequence: {commands}")
                if app.report_package_title_var.get() != "Waveform report unavailable.":
                    raise RuntimeError("Unavailable report UI title is not clean.")
                labels = [app.report_listbox.get(i) for i in range(app.report_listbox.size())]
                missing_noise = [label for label in labels if "Missing expected report files" in label or "waveform_samples" in label]
                if missing_noise:
                    raise RuntimeError(f"Unavailable UI contains missing-file noise: {missing_noise}")
            finally:
                app.outdir = old_outdir
                app.port_var.set(old_port)
                app.safe_199c_mode_var.set(old_safe)
                app.advanced_transfer_mode_var.set(old_advanced)
                app.serial_client = old_serial_client
                waveform_protocol.query_waveform = old_query_waveform
                app.instrument_profile = old_profile
                app.instrument_id = old_ident
                app.current_scope_baud = old_baud

        failures += 0 if check("196B legacy safe single waveform unavailable", safe_mode_single_waveform_unavailable_check) else 1

        def synthetic_196b_safe_single_waveform_success_check():
            from . import waveform_protocol
            import numpy as np

            old_outdir = app.outdir
            old_port = app.port_var.get()
            old_safe = app.safe_199c_mode_var.get()
            old_advanced = app.advanced_transfer_mode_var.get()
            old_serial_client = app.serial_client
            old_query_waveform = waveform_protocol.query_waveform
            old_profile = dict(app.instrument_profile)
            old_ident = app.instrument_id
            old_baud = app.current_scope_baud
            commands = []

            class FakeSer:
                is_open = True
                timeout = 1.0

                def flush(self):
                    pass

                def reset_input_buffer(self):
                    pass

                def reset_output_buffer(self):
                    pass

                def close(self):
                    self.is_open = False

                def read(self, _n=1):
                    return b""

            class FakeClient:
                baudrate = 1200

                def open(self):
                    return FakeSer()

                def query_ascii_allow_ack(self, ser, cmd, timeout=8.0, clear_input=True):
                    commands.append(cmd)
                    if cmd != "ID":
                        raise RuntimeError(f"Unexpected safe profile command: {cmd}")
                    return b"0", "FLUKE 196B;V06.12;2003-06-17"

                def send_cmd(self, ser, cmd, *args, **kwargs):
                    commands.append(cmd)
                    if cmd.startswith("GR") or cmd.startswith("PC"):
                        raise RuntimeError(f"Forbidden command in safe report: {cmd}")

            def fake_serial_client(baudrate=1200, xonxoff=False):
                if baudrate != 1200:
                    raise RuntimeError(f"Safe report should not open baud {baudrate}")
                return FakeClient()

            def fake_query_waveform(ser, client, trace_no):
                commands.append(f"QW {trace_no}")
                x = np.arange(300, dtype=float) * 0.0002
                phase = 0.0 if str(trace_no) == "10" else -0.2
                amp = 170.0 if str(trace_no) == "10" else 12.0
                y = amp * np.sin(2.0 * math.pi * 60.0 * x + phase)
                return {
                    "x": x,
                    "y": y,
                    "delta_x": 0.0002,
                    "n_points": len(x),
                    "adc_min": int(np.min(y)),
                    "adc_max": int(np.max(y)),
                    "volts_per_div": 200.0 if str(trace_no) == "10" else 20.0,
                    "time_per_div": 0.005,
                    "y_scale": 200.0 if str(trace_no) == "10" else 20.0,
                    "y_unit": "V" if str(trace_no) == "10" else "A",
                    "x_scale": 0.005,
                    "x_unit": "s",
                    "y_resolution": 8.0 if str(trace_no) == "10" else 0.8,
                    "sample_width": 1,
                    "samples_per_point": 1,
                    "trace_no": str(trace_no),
                }, b"synthetic-qw"

            try:
                app.outdir = tmp / "safe_single_success"
                app.outdir.mkdir(parents=True, exist_ok=True)
                app.latest_report_dir = None
                app.port_var.set("COM10")
                app.safe_199c_mode_var.set(True)
                app.advanced_transfer_mode_var.set(False)
                app.instrument_id = "FLUKE 196B;V06.12;2003-06-17"
                app.current_scope_baud = 1200
                app.update_instrument_profile(
                    ident=app.instrument_id,
                    port="COM10",
                    baud=1200,
                    safe_mode=True,
                    remote_used=False,
                )
                app.serial_client = fake_serial_client
                waveform_protocol.query_waveform = fake_query_waveform
                app.single_waveform_report()
                if any(cmd.startswith("GR") or cmd.startswith("PC") for cmd in commands):
                    raise RuntimeError(f"Forbidden command in 196B safe success path: {commands}")
                if commands != ["ID", "QW 10", "QW 20"]:
                    raise RuntimeError(f"Unexpected 196B safe success commands: {commands}")
                validate_single_waveform_report(app.latest_report_dir)
            finally:
                app.outdir = old_outdir
                app.port_var.set(old_port)
                app.safe_199c_mode_var.set(old_safe)
                app.advanced_transfer_mode_var.set(old_advanced)
                app.serial_client = old_serial_client
                waveform_protocol.query_waveform = old_query_waveform
                app.instrument_profile = old_profile
                app.instrument_id = old_ident
                app.current_scope_baud = old_baud

        failures += 0 if check("196B legacy safe single waveform report success", synthetic_196b_safe_single_waveform_success_check) else 1

        def timebase_correction_calibration_check():
            import numpy as np
            from .calibration import set_expected_line_frequency, set_timebase_correction
            from .frequency_tools import select_power_frequency

            old_correction = app.timebase_correction_var.get()
            old_expected = app.expected_line_frequency_var.get()
            old_settings_path = app.user_settings_path
            try:
                # Simulate a decoded 60 Hz signal whose time axis is compressed enough
                # to report about 66.67 Hz until the Fluke replay correction is applied.
                t_bad = np.arange(600, dtype=float) * 0.00015
                y = np.sin(2.0 * math.pi * 60.0 * (t_bad * 1.111111111))
                set_timebase_correction(1.0)
                set_expected_line_frequency(60.0)
                info_bad = select_power_frequency(t_bad, y)
                if not info_bad.get("calibration_suggestion"):
                    raise RuntimeError("Expected 66.67 Hz calibration suggestion was not generated.")

                t_fixed = t_bad * 1.111111111
                info_fixed = select_power_frequency(t_fixed, y)
                if abs(float(info_fixed["final_hz"]) - 60.0) > 0.1:
                    raise RuntimeError(f"Corrected frequency not near 60 Hz: {info_fixed['final_hz']}")

                app.user_settings_path = tmp / "scopesuite_user_settings.json"
                app.timebase_correction_var.set("1.111111111")
                app.expected_line_frequency_var.set("60.0")
                app.save_user_settings()
                app.timebase_correction_var.set("1.0")
                app.expected_line_frequency_var.set("50.0")
                app.load_user_settings()
                if app.timebase_correction_var.get() != "1.111111111":
                    raise RuntimeError("Saved timebase correction did not reload.")
                if app.expected_line_frequency_var.get() != "60.0":
                    raise RuntimeError("Saved expected line frequency did not reload.")
            finally:
                app.user_settings_path = old_settings_path
                app.timebase_correction_var.set(old_correction)
                app.expected_line_frequency_var.set(old_expected)
                app.apply_calibration_settings(save=False)

        failures += 0 if check("timebase correction calibration setting", timebase_correction_calibration_check) else 1

        deep_dir = tmp / "deep_memory"
        deep_dir.mkdir()
        rows = []
        for frame_idx, frame in enumerate(("replay_m02", "replay_m01", "replay_p00")):
            with (deep_dir / f"{frame}_waveforms.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "channel_a_voltage", "channel_b_current"])
                for sample_idx in range(160):
                    t = sample_idx / 2400.0
                    v = 120.0 * math.sqrt(2.0) * math.sin(2.0 * math.pi * 60.0 * t) * (1.0 - frame_idx * 0.04)
                    i = (8.0 + frame_idx * 3.0) * math.sqrt(2.0) * math.sin(2.0 * math.pi * 60.0 * t - 0.25)
                    writer.writerow([t, v, i])
            rows.append({
                "frame_name": frame,
                "replay_index": frame_idx - 2,
                "points_a": 160,
                "points_b": 160,
                "vrms_v": 120.0 * (1.0 - frame_idx * 0.04),
                "irms_a": 8.0 + frame_idx * 3.0,
                "real_power_w": 120.0 * (8.0 + frame_idx * 3.0) * 0.95,
                "apparent_power_va": 120.0 * (8.0 + frame_idx * 3.0),
                "reactive_power_var": 100.0,
                "power_factor": 0.95 - frame_idx * 0.08,
                "phase_i_minus_v_deg": -15.0,
                "dominant_freq_v_hz": 60.0 + frame_idx * 0.15,
                "dominant_freq_i_hz": 60.0 + frame_idx * 0.15,
                "thd_v": 0.02 + frame_idx * 0.01,
                "thd_i": 0.05 + frame_idx * 0.02,
                "harm3_i": 0.1,
                "harm5_i": 0.2,
                "harm7_i": 0.1,
                "waveform_csv": f"{frame}_waveforms.csv",
                "waveform_png": "",
                "fft_png": "",
                "raw_a_bin": "",
                "raw_b_bin": "",
            })
        with (deep_dir / "replay_summary.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        def deep_memory_check():
            outputs = analyze_deep_memory_capture(deep_dir, log=write)
            for key in ("full_csv", "trends_csv", "worst_csv", "summary_txt", "stitched_csv"):
                if not outputs.get(key) or not Path(outputs[key]).exists():
                    raise RuntimeError(f"Missing deep-memory output: {key}")
            for name in ("deep_memory_full_capture.png", "deep_memory_trends.png"):
                if not (deep_dir / name).exists():
                    raise RuntimeError(f"Missing deep-memory plot: {name}")

        failures += 0 if check("deep memory reconstruction synthetic", deep_memory_check) else 1

    if live_port:
        app.port_var.set(live_port)
        app.safe_199c_mode_var.set(True)
        failures += 0 if check(f"live {live_port} safe ID", app.test_connection) else 1
        failures += 0 if check(f"live {live_port} safe screen capture", app.capture_screen) else 1

    try:
        root.destroy()
    except Exception:
        pass

    write(f"RESULT {'PASS' if failures == 0 else 'FAIL'} failures={failures}")
    write(f"LOG {log_path}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    for result in assert_screen_capture_model_tests():
        print(
            "PASS "
            f"{result['model']}: mode={result['mode']} "
            f"png_supported={result['png_supported']} prefix={result['prefix']}"
        )
