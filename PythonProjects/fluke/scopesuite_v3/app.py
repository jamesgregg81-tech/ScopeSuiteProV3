import subprocess
import shutil
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

sys.dont_write_bytecode = True

from .config import (
    APP_NAME,
    BAUD,
    CURRENT_SCALE_A_PER_V,
    DEFAULT_OUTPUT_DIR,
    EXPECTED_SCREEN_SIZE,
    INITIAL_BAUD,
    TIMEBASE_CORRECTION,
    WORK_BAUD,
)
from .fft_tools import compute_fft_from_bytes
from .image_decoder import save_screen_debug_files
from .serial_client import FlukeSerialClient, available_ports, safe_release_scope


class FlukeScopeSuiteProV3:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1180x820")
        self.root.minsize(980, 680)

        self.outdir = DEFAULT_OUTPUT_DIR
        self.outdir.mkdir(parents=True, exist_ok=True)

        self.last_png = None
        self.last_raw = None
        self.last_waveform_raw = None
        self.last_fft = None
        self.image_ref = None
        self.report_image_refs = []
        self.latest_report_dir = None
        self.report_files = {}
        self.report_file_items = []
        self.report_package_title_var = tk.StringVar(value="No completed report package")
        self.report_package_detail_var = tk.StringVar(value="Run a replay export to generate an in-app report package.")
        self.active_serial = None
        self.active_client = None
        self.instrument_id = "Not connected"
        self.worker_running = False
        self.generator_site_vars = {}
        self.generator_factory_vars = {}
        self.generator_adjusted_vars = {}
        self.generator_result_vars = {}
        self.generator_evidence_dir_var = tk.StringVar(value="")
        self.generator_recovery_limit_var = tk.StringVar(value="5")
        self.generator_glove_mode_var = tk.BooleanVar(value=False)
        self.generator_sunlight_mode_var = tk.BooleanVar(value=True)
        self.generator_dark_mode_var = tk.BooleanVar(value=False)
        self.generator_status_vars = {}
        self.generator_detection_labels = {}

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_mode = tk.StringVar(value="determinate")
        self.progress_text = tk.StringVar(value="Idle")
        self.status_var = tk.StringVar(value="Ready")
        self.port_var = tk.StringVar()
        self.waveform_status = tk.StringVar(value="No waveform downloaded yet.")
        self.fft_status = tk.StringVar(value="Load or download a waveform, then compute FFT.")
        self.sample_rate_var = tk.StringVar(value="1.0")

        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)
        self.log_startup_info()
        self.refresh_ports()

    def build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        header = ttk.Frame(self.root, padding=(12, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text=APP_NAME, font=("Arial", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, anchor="e").grid(row=0, column=1, sticky="ew", padx=(16, 0))

        controls = ttk.LabelFrame(self.root, text="Connection", padding=10)
        controls.grid(row=1, column=0, sticky="ew", padx=12)
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Serial Port").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=42)
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(controls, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=3)
        ttk.Button(controls, text="Test", command=self.test_connection_thread).grid(row=0, column=3, padx=3)
        ttk.Button(controls, text="Choose Folder", command=self.choose_folder).grid(row=0, column=4, padx=3)
        ttk.Button(controls, text="Open Folder", command=self.open_folder).grid(row=0, column=5, padx=3)
        ttk.Button(controls, text="Open Last Report", command=self.open_last_report).grid(row=0, column=6, padx=3)

        progress_frame = ttk.Frame(self.root, padding=(12, 8, 12, 0))
        progress_frame.grid(row=2, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=28, anchor="e").grid(row=0, column=1, padx=(10, 0))

        self.tabs = ttk.Notebook(self.root)
        self.tabs.grid(row=3, column=0, sticky="nsew", padx=12, pady=10)

        self.build_capture_tab()
        self.build_waveform_tab()
        self.build_fft_tab()
        self.build_reports_tab()
        self.build_generator_reports_tab()
        self.build_settings_tab()
        self.build_log_tab()

    def build_capture_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.tabs.add(tab, text="Screen Capture")

        live = ttk.LabelFrame(tab, text="Live Scope Screen", padding=8)
        live.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(live, text="Capture Screen", command=self.capture_screen_thread).pack(side="left", padx=(0, 6))
        ttk.Button(live, text="Open Last Screen Image", command=self.open_last_image).pack(side="left", padx=6)

        replay = ttk.LabelFrame(tab, text="Replay / Debug Screen Files", padding=8)
        replay.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(replay, text="Replay Raw Screen Capture", command=self.replay_capture_thread).pack(side="left", padx=(0, 6))
        ttk.Button(replay, text="Replay Debug Screen Capture", command=self.replay_debug_capture_thread).pack(side="left", padx=6)

        image_frame = ttk.Frame(tab)
        image_frame.grid(row=2, column=0, sticky="nsew")
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)
        self.image_label = tk.Label(image_frame, bg="white", relief="sunken", anchor="center")
        self.image_label.grid(row=0, column=0, sticky="nsew")

    def build_waveform_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)
        self.tabs.add(tab, text="Waveform Capture")

        single = ttk.LabelFrame(tab, text="Single Capture", padding=8)
        single.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(single, text="Download Channel A Raw", command=lambda: self.waveform_thread("10")).pack(side="left", padx=(0, 6))
        ttk.Button(single, text="Download Channel B Raw", command=lambda: self.waveform_thread("20")).pack(side="left", padx=6)
        ttk.Button(single, text="Download Both Raw", command=self.waveform_both_thread).pack(side="left", padx=6)
        ttk.Button(single, text="Single Waveform Report", command=self.single_waveform_report_thread).pack(side="left", padx=6)

        replay = ttk.LabelFrame(tab, text="Replay Memory Export", padding=8)
        replay.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(replay, text="Export Replay Set", command=self.export_replay_thread).pack(side="left", padx=(0, 6))
        ttk.Button(replay, text="Stitched Replay View / Report", command=self.open_stitched_replay_view).pack(side="left", padx=6)
        ttk.Button(replay, text="Waterfall / Heatmap View", command=self.open_waterfall_replay_view).pack(side="left", padx=6)

        offline = ttk.LabelFrame(tab, text="Offline Files", padding=8)
        offline.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(offline, text="Load Raw Waveform", command=self.load_waveform_thread).pack(side="left", padx=(0, 6))
        ttk.Button(offline, text="Load Raw and FFT", command=self.load_waveform_and_fft_thread).pack(side="left", padx=6)

        ttk.Label(tab, textvariable=self.waveform_status).grid(row=3, column=0, sticky="w", pady=(0, 8))

        self.waveform_text = tk.Text(tab, height=12, wrap="word")
        self.waveform_text.grid(row=4, column=0, sticky="nsew")
        self.waveform_text.insert(
            "end",
            "Channel A = QW 10\n"
            "Channel B = QW 20\n\n"
            "Export Replay Set reads RP status, walks every replay frame in deep memory, "
            "and saves both channels for each frame.\n",
        )
        self.waveform_text.configure(state="disabled")

    def build_fft_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.tabs.add(tab, text="FFT")

        controls = ttk.LabelFrame(tab, text="Frequency Analysis", padding=8)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Sample rate").pack(side="left")
        ttk.Entry(controls, textvariable=self.sample_rate_var, width=10).pack(side="left", padx=6)
        ttk.Label(controls, text="Hz").pack(side="left")
        ttk.Button(controls, text="Compute FFT from Loaded Waveform", command=self.compute_fft_thread).pack(side="left", padx=12)
        ttk.Button(controls, text="Load Raw and FFT", command=self.load_waveform_and_fft_thread).pack(side="left", padx=6)
        ttk.Button(controls, text="Open Last FFT Plot", command=self.open_last_fft_plot).pack(side="left", padx=6)

        ttk.Label(tab, textvariable=self.fft_status).grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.fft_canvas = tk.Canvas(tab, bg="white", relief="sunken", height=360)
        self.fft_canvas.grid(row=2, column=0, sticky="nsew")
        self.fft_canvas.bind("<Configure>", lambda _event: self.draw_fft())

    def build_reports_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.reports_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        self.tabs.add(tab, text="Reports")

        package = ttk.LabelFrame(tab, text="Completed Report Package", padding=8)
        package.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        package.columnconfigure(0, weight=1)
        ttk.Label(package, textvariable=self.report_package_title_var, font=("Arial", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(package, textvariable=self.report_package_detail_var).grid(row=1, column=0, sticky="ew", pady=(3, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(actions, text="Report Actions").pack(side="left", padx=(0, 10))
        ttk.Button(actions, text="Open Professional Report", command=self.open_professional_report).pack(side="left", padx=6)
        ttk.Button(actions, text="Open Last Report", command=self.open_last_report).pack(side="left", padx=6)
        ttk.Button(actions, text="Open Selected File", command=self.open_selected_report_file).pack(side="left", padx=6)
        ttk.Button(actions, text="Save As", command=self.save_selected_report_as).pack(side="left", padx=6)
        ttk.Button(actions, text="Export PDF", command=self.export_professional_pdf).pack(side="left", padx=6)
        ttk.Button(actions, text="Copy Summary", command=self.copy_summary_to_clipboard).pack(side="left", padx=6)
        ttk.Button(actions, text="Export Folder", command=self.open_last_report).pack(side="left", padx=6)
        ttk.Button(actions, text="Refresh", command=self.refresh_reports_tab).pack(side="left", padx=6)

        self.report_status_var = tk.StringVar(value="No report session registered yet.")
        ttk.Label(tab, textvariable=self.report_status_var).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        left = ttk.Frame(tab)
        left.grid(row=3, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="Latest Report Package").grid(row=0, column=0, sticky="w")
        self.report_text = tk.Text(left, height=18, wrap="word")
        self.report_text.grid(row=1, column=0, sticky="nsew")
        self.report_text.configure(state="disabled")

        right = ttk.Frame(tab)
        right.grid(row=3, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        ttk.Label(right, text="Generated Files").grid(row=0, column=0, sticky="w")
        self.report_listbox = tk.Listbox(right, height=8)
        self.report_listbox.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        ttk.Label(right, text="Preview").grid(row=2, column=0, sticky="w")
        self.report_images_frame = ttk.Frame(right)
        self.report_images_frame.grid(row=3, column=0, sticky="nsew")
        ttk.Label(
            right,
            text="Advanced / Debug Files are kept in the report folder and hidden from the main list.",
        ).grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def build_generator_reports_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.generator_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)
        self.tabs.add(tab, text="Generator Reports")

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(top, text="Generator AVR / Governor Tuning", font=("Arial", 17, "bold")).pack(side="left")
        ttk.Label(
            top,
            text="Control adjustments must be performed by qualified personnel only.",
            foreground="#b00020",
            font=("Arial", 11, "bold"),
        ).pack(side="left", padx=18)

        modes = ttk.LabelFrame(tab, text="Field Modes", padding=8)
        modes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(modes, text="Glove Mode", variable=self.generator_glove_mode_var, command=self.apply_generator_modes).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(modes, text="Sunlight Mode", variable=self.generator_sunlight_mode_var, command=self.apply_generator_modes).pack(side="left", padx=14)
        ttk.Checkbutton(modes, text="Dark Theme", variable=self.generator_dark_mode_var, command=self.apply_generator_modes).pack(side="left", padx=14)
        ttk.Label(modes, text="Recovery limit").pack(side="left", padx=(26, 6))
        ttk.Entry(modes, textvariable=self.generator_recovery_limit_var, width=6).pack(side="left")
        ttk.Label(modes, text="sec").pack(side="left", padx=(4, 0))

        left = ttk.Frame(tab)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(tab)
        right.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)

        site = ttk.LabelFrame(left, text="Site / Generator Information", padding=8)
        site.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        fields = [
            ("customer", "Customer"),
            ("site_name", "Site Name"),
            ("generator_id", "Generator ID"),
            ("engine_model", "Engine Model"),
            ("alternator_model", "Alternator Model"),
            ("controller_type", "Controller Type"),
            ("kw_rating", "kW Rating"),
            ("kva_rating", "kVA Rating"),
            ("voltage", "Voltage"),
            ("phase", "Phase"),
            ("frequency", "Frequency"),
            ("technician", "Technician"),
            ("date", "Date"),
        ]
        for row, (key, label) in enumerate(fields):
            var = tk.StringVar(value=time.strftime("%Y-%m-%d") if key == "date" else ("60" if key == "frequency" else ""))
            self.generator_site_vars[key] = var
            ttk.Label(site, text=label).grid(row=row // 2, column=(row % 2) * 2, sticky="w", padx=(0, 5), pady=3)
            if key == "frequency":
                widget = ttk.Combobox(site, textvariable=var, values=("60", "50"), width=18)
            elif key == "phase":
                widget = ttk.Combobox(site, textvariable=var, values=("1", "3"), width=18)
            else:
                widget = ttk.Entry(site, textvariable=var, width=20)
            widget.grid(row=row // 2, column=(row % 2) * 2 + 1, sticky="ew", padx=(0, 10), pady=3)

        settings = ttk.LabelFrame(left, text="PowerCommand GOV / REG Settings", padding=8)
        settings.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        setting_rows = [
            ("gov_gain_percent", "GOV GAIN %"),
            ("gov_integral_percent", "GOV INTEGRAL %"),
            ("gov_ramp_sec", "GOV RAMP sec"),
            ("reg_gain_percent", "REG GAIN %"),
            ("reg_integral_percent", "REG INTEGRAL %"),
            ("reg_vhz", "REG VHZ"),
        ]
        ttk.Label(settings, text="Factory Defaults", font=("Arial", 11, "bold")).grid(row=0, column=1, padx=6)
        ttk.Label(settings, text="Adjusted Final Values", font=("Arial", 11, "bold")).grid(row=0, column=2, padx=6)
        for row, (key, label) in enumerate(setting_rows, start=1):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", pady=4)
            factory_var = tk.StringVar()
            adjusted_var = tk.StringVar()
            self.generator_factory_vars[key] = factory_var
            self.generator_adjusted_vars[key] = adjusted_var
            ttk.Entry(settings, textvariable=factory_var, width=18).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            ttk.Entry(settings, textvariable=adjusted_var, width=18).grid(row=row, column=2, sticky="ew", padx=6, pady=4)

        results = ttk.LabelFrame(left, text="Waveform Results / Manual Overrides", padding=8)
        results.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        result_rows = [
            ("no_load_voltage", "No-load voltage"),
            ("no_load_frequency", "No-load frequency"),
            ("full_load_frequency", "Full-load frequency"),
            ("max_voltage_dip_percent", "Voltage dip %"),
            ("max_frequency_dip_hz", "Frequency dip Hz"),
            ("recovery_time_sec", "Recovery time sec"),
            ("max_overshoot_percent", "Overshoot %"),
            ("settling_time_sec", "Settling time"),
            ("thd_under_load_percent", "THD under load %"),
            ("pf_under_load", "PF under load"),
            ("step_load_acceptance_score", "Step-load acceptance score"),
        ]
        for row, (key, label) in enumerate(result_rows):
            var = tk.StringVar()
            self.generator_result_vars[key] = var
            ttk.Label(results, text=label).grid(row=row // 2, column=(row % 2) * 2, sticky="w", padx=(0, 5), pady=3)
            ttk.Entry(results, textvariable=var, width=16).grid(row=row // 2, column=(row % 2) * 2 + 1, sticky="ew", padx=(0, 10), pady=3)

        actions = ttk.LabelFrame(right, text="Evidence and Report Actions", padding=10)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(actions, text="Capture Fluke Evidence", command=self.generator_capture_evidence_thread).grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        ttk.Button(actions, text="Use Latest Waveform Evidence", command=self.use_latest_generator_evidence).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(actions, text="Choose Evidence Folder", command=self.choose_generator_evidence_folder).grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        ttk.Button(actions, text="Generate Commissioning Report", command=self.generate_generator_report_thread).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(actions, text="Evidence folder").grid(row=2, column=0, sticky="w", padx=4, pady=(8, 0))
        ttk.Entry(actions, textvariable=self.generator_evidence_dir_var).grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        status = ttk.LabelFrame(right, text="Pass / Fail Status", padding=10)
        status.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for row, label in enumerate([
            "Frequency stable +/-0.5 Hz",
            "Voltage stable +/-2%",
            "Recovery under limit",
            "No sustained hunting",
            "No unstable AVR oscillation",
        ]):
            var = tk.StringVar(value="WAIT")
            self.generator_status_vars[label] = var
            ttk.Label(status, text=label).grid(row=row, column=0, sticky="w", pady=4)
            indicator = tk.Label(status, textvariable=var, width=10, bg="#666666", fg="white", font=("Arial", 12, "bold"))
            indicator.grid(row=row, column=1, sticky="e", padx=(12, 0), pady=4)
            self.generator_detection_labels[label] = indicator

        auto = ttk.LabelFrame(right, text="Automatic Detection", padding=10)
        auto.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for row, key in enumerate([
            "governor_hunting",
            "voltage_oscillation",
            "slow_recovery",
            "overdamped_response",
            "underdamped_response",
            "frequency_sag",
            "excess_thd",
        ]):
            label = key.replace("_", " ").title()
            var = tk.StringVar(value="WAIT")
            self.generator_status_vars[key] = var
            ttk.Label(auto, text=label).grid(row=row, column=0, sticky="w", pady=4)
            indicator = tk.Label(auto, textvariable=var, width=10, bg="#666666", fg="white", font=("Arial", 12, "bold"))
            indicator.grid(row=row, column=1, sticky="e", padx=(12, 0), pady=4)
            self.generator_detection_labels[key] = indicator

        self.generator_notes = tk.Text(right, height=8, wrap="word")
        self.generator_notes.grid(row=3, column=0, sticky="nsew")
        self.generator_notes.insert(
            "end",
            "Recommendations are generated from waveform evidence and manual measurements. "
            "Verify all AVR/governor changes against the generator manufacturer procedure.\n",
        )
        right.rowconfigure(3, weight=1)
        self.apply_generator_modes()

    def build_settings_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.tabs.add(tab, text="Settings")

        settings = ttk.LabelFrame(tab, text="Configuration", padding=8)
        settings.grid(row=0, column=0, sticky="nsew")
        settings.columnconfigure(0, weight=1)
        settings.rowconfigure(0, weight=1)
        self.settings_text = tk.Text(settings, height=18, wrap="word")
        self.settings_text.grid(row=0, column=0, sticky="nsew")

        actions = ttk.Frame(tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Refresh Settings Display", command=self.update_settings_display).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Choose Output Folder", command=self.choose_folder).pack(side="left", padx=6)
        ttk.Button(actions, text="Open Output Folder", command=self.open_folder).pack(side="left", padx=6)
        self.update_settings_display()

    def build_log_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.tabs.add(tab, text="Log")

        actions = ttk.LabelFrame(tab, text="Troubleshooting", padding=8)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(actions, text="Clear Log", command=self.clear_log).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Save Log", command=self.save_log).pack(side="left", padx=6)

        frame = ttk.Frame(tab)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=20, wrap="none")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    def ui_call(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def log_msg(self, msg):
        self.ui_call(self._log_msg, msg)

    def _log_msg(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        if hasattr(self, "log"):
            self.log.insert("end", f"[{timestamp}] {msg}\n")
            self.log.see("end")
        self.status_var.set(msg)

    def show_error(self, title, error):
        self.ui_call(messagebox.showerror, title, str(error))

    def log_startup_info(self):
        self.log_msg(f"Startup Python version: {sys.version.split()[0]}")
        self.log_msg(f"Startup executable: {sys.executable}")
        self.log_msg(f"Startup app package path: {Path(__file__).resolve().parent}")
        self.log_msg(f"Startup active output folder: {self.outdir}")

    def clear_log(self):
        if hasattr(self, "log"):
            self.log.delete("1.0", "end")
            self.status_var.set("Log cleared")

    def save_log(self):
        if not hasattr(self, "log"):
            return
        target = filedialog.asksaveasfilename(
            initialdir=str(self.outdir),
            initialfile=f"fluke_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if target:
            Path(target).write_text(self.log.get("1.0", "end"), encoding="utf-8")
            self.log_msg(f"Log saved: {target}")

    def track_serial_session(self, ser, client):
        self.active_serial = ser
        self.active_client = client

    def release_serial_session(self, ser=None, client=None):
        ser = ser or self.active_serial
        client = client or self.active_client
        if ser is None or client is None:
            self.log_msg("Release failed, port already closed")
            return
        safe_release_scope(ser, client, logger=self.log_msg)
        if ser is self.active_serial:
            self.active_serial = None
            self.active_client = None

    def on_app_close(self):
        if self.active_serial is not None and self.active_client is not None:
            self.release_serial_session(self.active_serial, self.active_client)
        self.root.destroy()

    def run_worker(self, target):
        if self.worker_running:
            messagebox.showinfo("Busy", "A capture or transfer is already running.")
            return

        self.worker_running = True

        def wrapped():
            try:
                target()
            finally:
                self.worker_running = False
                self.set_progress_idle()

        threading.Thread(target=wrapped, daemon=True).start()

    def set_progress(self, current=None, total=None, text=None, indeterminate=False):
        def update():
            if indeterminate:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                if total:
                    self.progress_var.set(min(100.0, max(0.0, current / total * 100.0)))
                elif current is not None:
                    self.progress_var.set(float(current))
            if text:
                self.progress_text.set(text)

        self.ui_call(update)

    def set_progress_idle(self):
        def update():
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress_var.set(0.0)
            self.progress_text.set("Idle")

        self.ui_call(update)

    def serial_client(self, baudrate=BAUD, xonxoff=False):
        return FlukeSerialClient(
            self.port_var.get().strip(),
            logger=self.log_msg,
            baudrate=baudrate,
            xonxoff=xonxoff,
        )

    def refresh_ports(self):
        ports = available_ports()
        self.port_combo["values"] = ports

        preferred = [
            p for p in ports
            if "usbserial" in p.lower() or "usbmodem" in p.lower()
        ]

        if preferred:
            self.port_var.set(preferred[0])
        elif ports and not self.port_var.get():
            self.port_var.set(ports[0])

        self.log_msg("Ports refreshed")
        self.update_settings_display()

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.outdir))
        if folder:
            self.outdir = Path(folder)
            self.outdir.mkdir(parents=True, exist_ok=True)
            self.log_msg(f"Save folder changed: {self.outdir}")
            self.update_settings_display()

    def open_folder(self):
        subprocess.run(["open", str(self.outdir)], check=False)

    def update_settings_display(self):
        port = self.port_var.get().strip() or "None selected"
        txt = (
            f"{APP_NAME}\n\n"
            f"Instrument: {self.instrument_id}\n"
            f"Serial Port: {port}\n"
            f"Baud Rate: {BAUD}\n"
            f"Serial Settings: 8 data bits, no parity, 1 stop bit\n"
            f"Flow Control: XON/XOFF enabled\n"
            f"Save Folder: {self.outdir}\n\n"
            "Waveform Scaling:\n"
            f"Current Probe Scaling: {CURRENT_SCALE_A_PER_V} A/V\n"
            f"Timebase Correction: {TIMEBASE_CORRECTION}\n\n"
            "Report Options:\n"
            "Professional HTML report: enabled\n"
            "Professional PDF report: enabled when ReportLab is available\n"
            "Operator: \n"
            "Job / Site: \n"
            "Notes: \n\n"
            "Safety:\n"
            "Do not change probes or input connections while connected to hazardous voltage.\n"
            "Use the Fluke input ratings and proper CAT-rated accessories.\n"
        )

        if hasattr(self, "settings_text"):
            self.settings_text.configure(state="normal")
            self.settings_text.delete("1.0", "end")
            self.settings_text.insert("end", txt)
            self.settings_text.configure(state="disabled")

    def apply_generator_modes(self):
        glove = self.generator_glove_mode_var.get()
        sunlight = self.generator_sunlight_mode_var.get()
        dark = self.generator_dark_mode_var.get()
        button_pad = 12 if glove else 6
        font_size = 14 if glove else 11
        bg = "#111111" if dark else ("#fffdf0" if sunlight else "#f5f5f5")
        fg = "#ffffff" if dark else "#111111"
        try:
            self.root.option_add("*TButton.padding", button_pad)
            self.root.option_add("*Font", ("Arial", font_size))
        except Exception:
            pass
        if hasattr(self, "generator_tab"):
            try:
                self.generator_tab.configure(style="Generator.TFrame")
                style = ttk.Style()
                style.configure("Generator.TFrame", background=bg)
                style.configure("Generator.TLabelframe", background=bg, foreground=fg)
            except Exception:
                pass
        self.log_msg(
            "Generator field mode updated: "
            f"glove={glove}, sunlight={sunlight}, dark={dark}"
        )

    def generator_dict(self, vars_by_key):
        return {key: var.get().strip() for key, var in vars_by_key.items()}

    def choose_generator_evidence_folder(self):
        initial = str(self.latest_report_dir or self.outdir)
        folder = filedialog.askdirectory(initialdir=initial)
        if folder:
            self.generator_evidence_dir_var.set(folder)
            self.log_msg(f"Generator evidence folder selected: {folder}")

    def use_latest_generator_evidence(self):
        report_dir = self.latest_report_dir or self.discover_latest_report_dir()
        if report_dir and Path(report_dir).exists():
            self.generator_evidence_dir_var.set(str(report_dir))
            self.log_msg(f"Generator evidence set to latest report package: {report_dir}")
        else:
            messagebox.showinfo("No Evidence", "Generate or capture waveform evidence first.")

    def generator_capture_evidence_thread(self):
        self.run_worker(self.generator_capture_evidence)

    def generator_capture_evidence(self):
        before = self.latest_report_dir
        self.log_msg("Generator evidence capture: starting single waveform report from Fluke.")
        self.single_waveform_report()
        if self.latest_report_dir and self.latest_report_dir != before:
            self.generator_evidence_dir_var.set(str(self.latest_report_dir))
            self.log_msg(f"Generator evidence captured: {self.latest_report_dir}")
        elif self.latest_report_dir:
            self.generator_evidence_dir_var.set(str(self.latest_report_dir))
            self.log_msg(f"Generator evidence available: {self.latest_report_dir}")

    def generate_generator_report_thread(self):
        self.run_worker(self.generate_generator_report)

    def generate_generator_report(self):
        try:
            self.set_progress(text="Generator report...", indeterminate=True)
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            report_dir = self.outdir / "reports" / f"generator_{ts}"
            report_dir.mkdir(parents=True, exist_ok=True)

            site = self.generator_dict(self.generator_site_vars)
            factory = self.generator_dict(self.generator_factory_vars)
            adjusted = self.generator_dict(self.generator_adjusted_vars)
            results = self.generator_dict(self.generator_result_vars)
            evidence_dir = self.generator_evidence_dir_var.get().strip()
            options = {
                "recovery_limit_sec": self.generator_recovery_limit_var.get().strip() or "5",
                "sunlight_mode": self.generator_sunlight_mode_var.get(),
            }

            from .generator_report import build_generator_commissioning_report

            package = build_generator_commissioning_report(
                report_dir,
                site,
                factory,
                adjusted,
                results,
                evidence_dir=evidence_dir,
                options=options,
                log=self.log_msg,
            )
            self.update_generator_indicators(package.get("status", {}), package.get("diagnostics", {}))
            self.register_report_session(report_dir, announce=True)
            self.set_progress(100, 100, "Generator report complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Generator Report Error", exc)

    def update_generator_indicators(self, status, diagnostics):
        def set_indicator(key, passed):
            label = self.generator_detection_labels.get(key)
            var = self.generator_status_vars.get(key)
            if not label or not var:
                return
            if passed is None:
                text, color = "WAIT", "#666666"
            elif passed:
                text, color = "PASS", "#0a7f2e"
            else:
                text, color = "FAIL", "#b00020"
            var.set(text)
            label.configure(bg=color)

        for key, value in status.items():
            display_key = "Recovery under limit" if key.startswith("Recovery under") else key
            set_indicator(display_key, bool(value))
        for key, value in diagnostics.items():
            label = self.generator_detection_labels.get(key)
            var = self.generator_status_vars.get(key)
            if label and var:
                if value:
                    var.set("YES")
                    label.configure(bg="#b00020")
                else:
                    var.set("NO")
                    label.configure(bg="#0a7f2e")

    def test_connection_thread(self):
        self.run_worker(self.test_connection)

    def test_connection(self):
        ser = None
        client = None
        try:
            self.set_progress(text="Testing...", indeterminate=True)
            client = self.serial_client()
            ser = client.open()
            self.track_serial_session(ser, client)
            ident = client.enter_remote_and_identify(ser)
            self.instrument_id = ident
            self.log_msg(f"Instrument: {ident}")
            self.ui_call(self.update_settings_display)
            self.log_msg("Connection test passed")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Connection Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def capture_screen_thread(self):
        self.run_worker(self.capture_screen)

    def single_waveform_report_thread(self):
        self.run_worker(self.single_waveform_report)

    def capture_screen(self):
        ser = None
        client = None
        try:
            self.log_msg("Starting screen capture...")
            self.set_progress(0, 100, "Capture 0%")

            ts = time.strftime("%Y%m%d_%H%M%S")
            raw_file = self.outdir / f"fluke199c_screen_{ts}.bin"
            png_file = self.outdir / f"fluke199c_screen_{ts}.png"

            ser, client, ident = self.connect_screen_serial()
            self.track_serial_session(ser, client)
            self.instrument_id = ident
            self.log_msg(f"Instrument: {ident}")
            self.ui_call(self.update_settings_display)
            raw = self.capture_screen_bytes(ser, client)

            raw_file.write_bytes(raw)
            self.last_raw = raw_file
            self.log_msg(f"Raw screen saved: {raw_file}")

            debug = self.save_screen_debug(raw, self.outdir)
            Image.open(debug["decoded_path"]).save(png_file)
            self.last_png = png_file

            self.log_msg(f"PNG saved: {png_file}")
            self.log_screen_debug(debug)
            self.register_screen_capture_report(png_file, debug)
            self.show_image(png_file)
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Capture Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def single_waveform_report(self):
        ser = None
        client = None
        export_dir = None
        try:
            self.log_msg("Starting single screen waveform report...")
            self.set_progress(0, 100, "Single report 0%")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            export_dir = self.outdir / "reports" / f"single_{ts}"
            export_dir.mkdir(parents=True, exist_ok=True)
            self.log_msg(f"Report output folder: {export_dir}")

            ser, client, ident = self.connect_screen_serial()
            self.track_serial_session(ser, client)
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)

            raw = self.capture_screen_bytes(ser, client)
            debug = self.save_screen_debug(raw, export_dir)
            screen_capture = export_dir / "screen_capture.png"
            Image.open(debug["decoded_path"]).save(screen_capture)
            self.last_png = screen_capture
            self.last_raw = debug["raw_path"]
            self.log_screen_debug(debug)
            self.show_image(screen_capture)
            self.set_progress(35, 100, "Screen captured")

            wf_a = None
            wf_b = None
            q_w_errors = []
            try:
                from .waveform_protocol import query_waveform
                self.log_msg("Querying active Channel A numeric waveform: QW 10")
                wf_a, raw_a = query_waveform(ser, client, "10")
                (export_dir / "single_waveform_A_raw.bin").write_bytes(raw_a)
                self.set_progress(60, 100, "Channel A captured")
            except Exception as exc:
                q_w_errors.append(f"Channel A QW failed: {exc}")
                self.log_msg(q_w_errors[-1])

            try:
                from .waveform_protocol import query_waveform
                self.log_msg("Querying active Channel B numeric waveform: QW 20")
                wf_b, raw_b = query_waveform(ser, client, "20")
                (export_dir / "single_waveform_B_raw.bin").write_bytes(raw_b)
                self.set_progress(80, 100, "Channel B captured")
            except Exception as exc:
                q_w_errors.append(f"Channel B QW failed: {exc}")
                self.log_msg(q_w_errors[-1])

            try:
                from .waveform_protocol import save_single_waveform_report
                visual_only = wf_a is None and wf_b is None
                report_path = save_single_waveform_report(
                    export_dir,
                    ident,
                    screen_capture,
                    wf_a=wf_a,
                    wf_b=wf_b,
                    visual_only=visual_only,
                    q_w_error="; ".join(q_w_errors) if q_w_errors else None,
                    log=self.log_msg,
                )
            except Exception as exc:
                report_path = export_dir / "SINGLE_WAVEFORM_REPORT.txt"
                report_path.write_text(
                    "\n".join([
                        "SINGLE SCREEN WAVEFORM REPORT",
                        "=============================",
                        "",
                        f"Scope ID: {ident}",
                        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        f"Screenshot reference image: {screen_capture.name}",
                        "",
                        "Visual approximation only -- numeric waveform data unavailable.",
                        f"Report generation fallback reason: {exc}",
                        f"QW errors: {'; '.join(q_w_errors) if q_w_errors else 'Unavailable'}",
                    ]),
                    encoding="utf-8",
                )
                self.log_msg(f"Numeric single report failed, wrote screenshot-only report: {exc}")

            self.log_msg(f"Single waveform report saved: {report_path}")
            self.build_professional_report(export_dir, ident, "Single Screen Waveform Report")
            self.register_report_session(export_dir, announce=True)
            self.set_progress(100, 100, "Single report complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            if export_dir:
                try:
                    self.register_report_session(export_dir, announce=True)
                except Exception:
                    pass
            self.show_error("Single Waveform Report Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def capture_screen_bytes(self, ser, client):
        try:
            self.log_msg("Requesting 199C color PNG screen: QP 0,11,B")
            client.send_cmd(ser, "QP 0,11,B")
            return client.read_qp_png_payload(ser, self.progress_callback("Capture"))
        except Exception as exc:
            self.log_msg(f"Color PNG capture failed, trying legacy Epson capture: {exc}")
            client.send_cmd(ser, "QP")
            return client.read_qp_payload(ser, self.progress_callback("Capture"))

    def connect_screen_serial(self):
        last_error = None
        for baudrate in (BAUD, WORK_BAUD):
            client = self.serial_client(baudrate=baudrate, xonxoff=True)
            ser = None
            try:
                ser = client.open()
                time.sleep(0.5)
                ident = client.enter_remote_and_identify(ser)
                return ser, client, ident
            except Exception as exc:
                last_error = exc
                self.log_msg(f"Screen capture connect failed at {baudrate} baud: {exc}")
                if ser is not None:
                    self.release_serial_session(ser, client)
        raise last_error or RuntimeError("Unable to connect for screen capture.")

    def progress_callback(self, label):
        def callback(current, total):
            if total:
                percent = current / total * 100.0
                text = f"{label} {percent:.0f}%"
                self.set_progress(current, total, text)
            else:
                self.set_progress(text=f"{label}: {current} bytes", indeterminate=True)

        return callback

    def replay_capture_thread(self):
        path = filedialog.askopenfilename(
            initialdir=str(self.outdir),
            title="Replay raw screen capture",
            filetypes=(("Raw captures", "*.bin"), ("All files", "*.*")),
        )
        if not path:
            return
        self.run_worker(lambda: self.replay_capture(Path(path)))

    def replay_debug_capture_thread(self):
        path = self.outdir / "raw_capture.bin"
        if not path.exists():
            messagebox.showinfo("No Debug Capture", f"No debug capture found at {path}")
            return
        self.run_worker(lambda: self.replay_capture(path))

    def replay_capture(self, raw_path):
        try:
            self.set_progress(text="Replay...", indeterminate=True)
            raw = raw_path.read_bytes()
            png_file = raw_path.with_suffix(".replay.png")
            debug = self.save_screen_debug(raw, raw_path.parent)
            Image.open(debug["decoded_path"]).save(png_file)
            self.last_raw = raw_path
            self.last_png = png_file
            self.log_msg(f"Replay decoded: {png_file}")
            self.log_screen_debug(debug)
            self.show_image(png_file)
            self.set_progress(100, 100, "Replay complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Replay Error", exc)

    def save_screen_debug(self, raw, output_dir):
        return save_screen_debug_files(
            raw,
            output_dir,
            expected_size=EXPECTED_SCREEN_SIZE,
            preview_max_size=self.preview_max_size(),
        )

    def preview_max_size(self):
        width = max(320, self.image_label.winfo_width() - 30)
        height = max(240, self.image_label.winfo_height() - 30)
        return width, height

    def log_screen_debug(self, debug):
        self.log_msg(f"Screen source format: {debug['source_format']}")
        self.log_msg(f"Raw byte count received: {debug['raw_byte_count']}")
        self.log_msg(f"Decoded width/height: {debug['decoded_size'][0]} x {debug['decoded_size'][1]}")
        self.log_msg(f"Expected width/height: {debug['expected_size'][0]} x {debug['expected_size'][1]}")
        self.log_msg(f"Crop rectangle: {debug['crop_rect']}")
        self.log_msg(f"Final rendered image size: {debug['rendered_size'][0]} x {debug['rendered_size'][1]}")
        self.log_msg(f"Debug raw: {debug['raw_path']}")
        self.log_msg(f"Debug decoded full: {debug['decoded_path']}")
        self.log_msg(f"Debug rendered preview: {debug['preview_path']}")
        if debug["decoded_size"] == debug["expected_size"]:
            self.log_msg("Screen Capture Test PASS: full LCD image dimensions match expected display size.")
        else:
            self.log_msg("Screen Capture Test WARN: decoded size does not match expected full LCD dimensions.")

    def show_image(self, path):
        img = Image.open(path).convert("RGB")
        max_w = max(320, self.image_label.winfo_width() - 30)
        max_h = max(240, self.image_label.winfo_height() - 30)
        scale = min(max_w / img.width, max_h / img.height, 1.0)
        display_size = (int(img.width * scale), int(img.height * scale))
        img = img.resize(display_size)

        def update():
            photo = ImageTk.PhotoImage(img)
            self.image_ref = photo
            self.image_label.config(image=self.image_ref)

        self.ui_call(update)

    def open_last_image(self):
        if self.last_png and self.last_png.exists():
            subprocess.run(["open", str(self.last_png)], check=False)
        else:
            messagebox.showinfo("No Image", "No captured image yet.")

    def waveform_thread(self, trace_no):
        self.run_worker(lambda: self.download_waveform_raw(trace_no))

    def waveform_both_thread(self):
        self.run_worker(self.download_both_waveforms_raw)

    def download_both_waveforms_raw(self):
        self.download_waveform_raw("10")
        self.download_waveform_raw("20")

    def download_waveform_raw(self, trace_no):
        ser = None
        client = None
        try:
            self.log_msg(f"Starting raw waveform download for trace {trace_no}...")
            self.set_progress(text="Waveform...", indeterminate=True)

            ts = time.strftime("%Y%m%d_%H%M%S")
            name = "A" if trace_no == "10" else "B" if trace_no == "20" else trace_no
            raw_file = self.outdir / f"fluke199c_waveform_{name}_{ts}.bin"

            client = self.serial_client()
            ser = client.open()
            self.track_serial_session(ser, client)
            ident = client.enter_remote_and_identify(ser)
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)
            client.send_cmd(ser, f"QW {trace_no}")
            raw = client.read_waveform_response_raw(ser, self.progress_callback("Waveform"))

            raw_file.write_bytes(raw)
            self.last_raw = raw_file
            self.last_waveform_raw = raw_file
            self.ui_call(self.waveform_status.set, f"Saved trace {trace_no}: {raw_file.name} ({len(raw)} bytes)")
            self.log_msg(f"Waveform raw saved: {raw_file}")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Waveform Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def export_replay_thread(self):
        self.run_worker(self.export_replay_set)

    def replay_progress(self, current, total, text):
        self.set_progress(current, total, text)
        self.ui_call(self.waveform_status.set, text)

    def connect_replay_serial(self):
        port = self.port_var.get().strip()
        if not port:
            raise RuntimeError("No serial port selected.")

        client = self.serial_client(INITIAL_BAUD)
        ser = client.open()
        self.track_serial_session(ser, client)
        time.sleep(0.5)

        try:
            ident = client.query_ascii(ser, "ID")
            self.log_msg(f"Instrument: {ident}")
        except Exception:
            self.release_serial_session(ser, client)
            self.log_msg(f"ID failed at {INITIAL_BAUD}; trying {WORK_BAUD} baud")
            client = self.serial_client(WORK_BAUD)
            ser = client.open()
            self.track_serial_session(ser, client)
            time.sleep(0.5)
            ident = client.query_ascii(ser, "ID")
            self.log_msg(f"Instrument: {ident}")
            return ser, client, ident

        if WORK_BAUD != INITIAL_BAUD:
            try:
                client.send_cmd(ser, f"PC {WORK_BAUD}")
            except Exception as exc:
                self.log_msg(f"Baud switch failed, continuing at {INITIAL_BAUD}: {exc}")
            else:
                try:
                    ser.flush()
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception:
                    pass
                ser.close()
                self.log_msg("Serial port closed")
                if ser is self.active_serial:
                    self.active_serial = None
                    self.active_client = None
                time.sleep(0.7)
                client = self.serial_client(WORK_BAUD)
                ser = client.open()
                self.track_serial_session(ser, client)
                time.sleep(0.5)
                ident = client.query_ascii(ser, "ID")
                self.log_msg(f"Reconnected at {WORK_BAUD}: {ident}")

        return ser, client, ident

    def export_replay_set(self):
        ser = None
        client = None
        try:
            self.log_msg("Starting deep-memory replay export...")
            self.set_progress(0, 100, "Replay 0%")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            export_dir = self.outdir / "reports" / ts
            export_dir.mkdir(parents=True, exist_ok=True)
            self.log_msg(f"Report output folder: {export_dir}")

            ser, client, ident = self.connect_replay_serial()
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)

            from .waveform_protocol import export_replay_waveforms

            rows, rp_count = export_replay_waveforms(
                ser,
                client,
                export_dir,
                ident,
                progress=self.replay_progress,
                log=self.log_msg,
            )

            msg = f"Replay export complete: {len(rows)} / {rp_count} frames saved to {export_dir.name}"
            self.ui_call(self.waveform_status.set, msg)
            self.log_msg(msg)
            self.last_raw = export_dir / "replay_summary.csv"
            self.build_professional_report(export_dir, ident, "Replay Export")
            self.finalize_report_session(export_dir)
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Replay Export Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def finalize_report_session(self, report_dir):
        report_dir = Path(report_dir)
        generated = sorted(p for p in report_dir.iterdir() if p.is_file())
        self.log_msg(f"Report files generated: {', '.join(p.name for p in generated) if generated else 'none'}")

        summary_csv = report_dir / "replay_summary.csv"
        waveform_csv = report_dir / "waveform_export.csv"
        if summary_csv.exists():
            shutil.copyfile(summary_csv, waveform_csv)
            self.log_msg(f"Registered waveform export CSV alias: {waveform_csv}")

        screen_target = report_dir / "screen_capture.png"
        screen_source = None
        if self.last_png and Path(self.last_png).exists():
            screen_source = Path(self.last_png)
        elif (self.outdir / "decoded_full.png").exists():
            screen_source = self.outdir / "decoded_full.png"

        if screen_source:
            shutil.copyfile(screen_source, screen_target)
            self.log_msg(f"Registered latest screen image: {screen_target}")

        self.register_report_session(report_dir, announce=True)

    def build_professional_report(self, report_dir, ident, report_type):
        try:
            from .professional_report import build_professional_report_package
            build_professional_report_package(report_dir, ident, report_type, log=self.log_msg)
        except Exception as exc:
            self.log_msg(f"Professional report generation failed: {exc}")

    def register_screen_capture_report(self, png_file, debug):
        if not self.latest_report_dir:
            return
        report_dir = Path(self.latest_report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        screen_target = report_dir / "screen_capture.png"
        shutil.copyfile(png_file, screen_target)
        self.log_msg(f"Registered screen capture in report UI: {screen_target}")
        self.register_report_session(report_dir)

    def register_report_session(self, report_dir, announce=False):
        report_dir = Path(report_dir)
        single_report = report_dir / "SINGLE_WAVEFORM_REPORT.txt"
        generator_report = report_dir / "GENERATOR_COMMISSIONING_REPORT.html"
        is_single = single_report.exists() or report_dir.name.startswith("single_")
        is_generator = generator_report.exists() or report_dir.name.startswith("generator_")
        if is_generator:
            expected = [
                generator_report,
                report_dir / "GENERATOR_COMMISSIONING_REPORT.txt",
                report_dir / "settings_summary.csv",
                report_dir / "waveform_evidence",
                report_dir / "screenshots",
                report_dir / "plots",
            ]
        elif is_single:
            expected = [
                single_report,
                report_dir / "screen_capture.png",
                report_dir / "PROFESSIONAL_REPORT.html",
                report_dir / "SUMMARY_METRICS.csv",
                report_dir / "waveform_samples.csv",
                report_dir / "waveform_plot.png",
                report_dir / "fft_spectrum.png",
                report_dir / "harmonic_summary.png",
            ]
        else:
            expected = [
                report_dir / "FINAL_GLOBAL_REPORT.txt",
                report_dir / "global_trend_summary.png",
                report_dir / "global_harmonic_summary.png",
                report_dir / "screen_capture.png",
                report_dir / "waveform_export.csv",
                report_dir / "stitched_replay_waveforms.csv",
                report_dir / "stitched_replay_overview.png",
                report_dir / "waterfall_replay_heatmap.png",
                report_dir / "PROFESSIONAL_REPORT.html",
                report_dir / "SUMMARY_METRICS.csv",
                report_dir / "waveform_samples.csv",
                report_dir / "waveform_plot.png",
                report_dir / "fft_spectrum.png",
                report_dir / "harmonic_summary.png",
            ]

        files = {
            "professional_html": report_dir / "PROFESSIONAL_REPORT.html",
            "professional_pdf": report_dir / "PROFESSIONAL_REPORT.pdf",
            "summary_metrics": report_dir / "SUMMARY_METRICS.csv",
            "waveform_samples": report_dir / "waveform_samples.csv",
            "professional_waveform_plot": report_dir / "waveform_plot.png",
            "professional_fft_plot": report_dir / "fft_spectrum.png",
            "professional_harmonic_plot": report_dir / "harmonic_summary.png",
            "generator_html": generator_report,
            "generator_pdf": report_dir / "GENERATOR_COMMISSIONING_REPORT.pdf",
            "generator_text": report_dir / "GENERATOR_COMMISSIONING_REPORT.txt",
            "generator_settings_csv": report_dir / "settings_summary.csv",
            "generator_voltage_plot": report_dir / "plots" / "voltage_load_step.png",
            "generator_frequency_plot": report_dir / "plots" / "frequency_load_step.png",
            "generator_current_plot": report_dir / "plots" / "current_inrush.png",
            "generator_recovery_plot": report_dir / "plots" / "recovery_curve.png",
            "final_report": report_dir / "FINAL_GLOBAL_REPORT.txt",
            "single_report": single_report,
            "trend_plot": report_dir / "global_trend_summary.png",
            "harmonic_plot": report_dir / "global_harmonic_summary.png",
            "screen_capture": report_dir / "screen_capture.png",
            "waveform_export": report_dir / "waveform_export.csv",
            "stitched_csv": report_dir / "stitched_replay_waveforms.csv",
            "stitched_plot": report_dir / "stitched_replay_overview.png",
            "waterfall_plot": report_dir / "waterfall_replay_heatmap.png",
            "single_waveform_plot": report_dir / "single_waveform_plot.png",
            "single_fft_plot": report_dir / "single_fft_plot.png",
            "csv_files": sorted(report_dir.glob("*.csv")),
        }

        missing = [p for p in expected if not p.exists()]
        registered = []
        for value in files.values():
            if isinstance(value, list):
                registered.extend(p for p in value if p.exists())
            elif value.exists():
                registered.append(value)

        self.latest_report_dir = report_dir
        self.report_files = files
        self.log_msg(f"Report output folder: {report_dir}")
        self.log_msg(f"Files registered in UI: {', '.join(p.name for p in registered) if registered else 'none'}")
        if missing:
            self.log_msg(f"Missing expected report files: {', '.join(p.name for p in missing)}")
        else:
            self.log_msg("Missing expected report files: none")

        self.ui_call(self.refresh_reports_tab)
        if announce:
            self.surface_completed_report_package(report_dir, registered, missing)

    def surface_completed_report_package(self, report_dir, registered, missing):
        def update():
            title = "Report package complete"
            detail = f"{len(registered)} files registered for this session: {report_dir}"
            if missing:
                detail += f" | Missing: {', '.join(p.name for p in missing)}"

            self.refresh_reports_tab()
            self.report_package_title_var.set(title)
            self.report_package_detail_var.set(detail)
            self.report_status_var.set(detail)

            if hasattr(self, "report_listbox") and self.report_listbox.size() > 0:
                self.report_listbox.selection_clear(0, "end")
                self.report_listbox.selection_set(0)
                self.report_listbox.activate(0)

            if hasattr(self, "reports_tab"):
                self.tabs.select(self.reports_tab)

            self.status_var.set("Report package complete - available in Reports")

        self.ui_call(update)

    def discover_latest_report_dir(self):
        reports_root = self.outdir / "reports"
        if not reports_root.exists():
            return None
        candidates = sorted([p for p in reports_root.iterdir() if p.is_dir()])
        return candidates[-1] if candidates else None

    def refresh_reports_tab(self):
        if not self.latest_report_dir:
            latest = self.discover_latest_report_dir()
            if latest:
                self.register_report_session(latest)
            return

        report_dir = Path(self.latest_report_dir)
        self.report_files = {
            "professional_html": report_dir / "PROFESSIONAL_REPORT.html",
            "professional_pdf": report_dir / "PROFESSIONAL_REPORT.pdf",
            "summary_metrics": report_dir / "SUMMARY_METRICS.csv",
            "waveform_samples": report_dir / "waveform_samples.csv",
            "professional_waveform_plot": report_dir / "waveform_plot.png",
            "professional_fft_plot": report_dir / "fft_spectrum.png",
            "professional_harmonic_plot": report_dir / "harmonic_summary.png",
            "generator_html": report_dir / "GENERATOR_COMMISSIONING_REPORT.html",
            "generator_pdf": report_dir / "GENERATOR_COMMISSIONING_REPORT.pdf",
            "generator_text": report_dir / "GENERATOR_COMMISSIONING_REPORT.txt",
            "generator_settings_csv": report_dir / "settings_summary.csv",
            "generator_voltage_plot": report_dir / "plots" / "voltage_load_step.png",
            "generator_frequency_plot": report_dir / "plots" / "frequency_load_step.png",
            "generator_current_plot": report_dir / "plots" / "current_inrush.png",
            "generator_recovery_plot": report_dir / "plots" / "recovery_curve.png",
            "final_report": report_dir / "FINAL_GLOBAL_REPORT.txt",
            "single_report": report_dir / "SINGLE_WAVEFORM_REPORT.txt",
            "trend_plot": report_dir / "global_trend_summary.png",
            "harmonic_plot": report_dir / "global_harmonic_summary.png",
            "screen_capture": report_dir / "screen_capture.png",
            "waveform_export": report_dir / "waveform_export.csv",
            "stitched_csv": report_dir / "stitched_replay_waveforms.csv",
            "stitched_plot": report_dir / "stitched_replay_overview.png",
            "waterfall_plot": report_dir / "waterfall_replay_heatmap.png",
            "single_waveform_plot": report_dir / "single_waveform_plot.png",
            "single_fft_plot": report_dir / "single_fft_plot.png",
            "csv_files": sorted(report_dir.glob("*.csv")),
        }
        if hasattr(self, "report_status_var"):
            self.report_status_var.set(f"Latest report bundle: {report_dir}")
        if hasattr(self, "report_package_title_var"):
            self.report_package_title_var.set("Report package loaded")
            self.report_package_detail_var.set(f"Viewing report package: {report_dir}")

        if hasattr(self, "report_text"):
            final_report = self.report_files.get("final_report", report_dir / "FINAL_GLOBAL_REPORT.txt")
            if not final_report.exists():
                final_report = self.report_files.get("single_report", report_dir / "SINGLE_WAVEFORM_REPORT.txt")
            if not final_report.exists():
                final_report = self.report_files.get("generator_text", report_dir / "GENERATOR_COMMISSIONING_REPORT.txt")
            self.report_text.configure(state="normal")
            self.report_text.delete("1.0", "end")
            if final_report.exists():
                self.report_text.insert("end", final_report.read_text(encoding="utf-8", errors="replace"))
            else:
                self.report_text.insert("end", "No text report is available for this session.")
            self.report_text.configure(state="disabled")

        if hasattr(self, "report_listbox"):
            self.report_listbox.delete(0, "end")
            self.report_file_items = []
            paths = []
            for key in (
                "generator_html", "generator_pdf", "generator_text", "generator_settings_csv",
                "generator_voltage_plot", "generator_frequency_plot", "generator_current_plot", "generator_recovery_plot",
                "professional_html", "professional_pdf", "summary_metrics", "waveform_samples",
                "professional_waveform_plot", "professional_fft_plot", "professional_harmonic_plot",
                "final_report", "single_report", "trend_plot", "harmonic_plot", "stitched_plot",
                "waterfall_plot", "single_waveform_plot", "single_fft_plot",
                "screen_capture", "waveform_export", "stitched_csv",
            ):
                path = self.report_files.get(key)
                if path and Path(path).exists():
                    paths.append(Path(path))
            paths.extend(p for p in self.report_files.get("csv_files", []) if p.exists() and p not in paths)
            for path in paths:
                self.report_file_items.append(path)
                self.report_listbox.insert("end", path.name)

        if hasattr(self, "report_images_frame"):
            for child in self.report_images_frame.winfo_children():
                child.destroy()
            self.report_image_refs = []
            image_paths = [
                self.report_files.get("generator_voltage_plot"),
                self.report_files.get("generator_frequency_plot"),
                self.report_files.get("generator_current_plot"),
                self.report_files.get("generator_recovery_plot"),
                self.report_files.get("professional_waveform_plot"),
                self.report_files.get("professional_fft_plot"),
                self.report_files.get("professional_harmonic_plot"),
                self.report_files.get("trend_plot"),
                self.report_files.get("harmonic_plot"),
                self.report_files.get("stitched_plot"),
                self.report_files.get("waterfall_plot"),
                self.report_files.get("single_waveform_plot"),
                self.report_files.get("single_fft_plot"),
                self.report_files.get("screen_capture"),
            ]
            for idx, path in enumerate(p for p in image_paths if p and Path(p).exists()):
                img = Image.open(path).convert("RGB")
                img.thumbnail((320, 190))
                photo = ImageTk.PhotoImage(img)
                self.report_image_refs.append(photo)
                frame = ttk.Frame(self.report_images_frame, padding=(0, 0, 0, 8))
                frame.grid(row=idx, column=0, sticky="ew")
                ttk.Label(frame, text=Path(path).name).pack(anchor="w")
                ttk.Label(frame, image=photo).pack(anchor="w")

    def selected_report_path(self):
        if not hasattr(self, "report_listbox"):
            return None
        selection = self.report_listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if index >= len(self.report_file_items):
            return None
        return self.report_file_items[index]

    def open_selected_report_file(self):
        path = self.selected_report_path()
        if not path:
            messagebox.showinfo("No Report File", "Select a report file first.")
            return
        subprocess.run(["open", str(path)], check=False)

    def save_selected_report_as(self):
        path = self.selected_report_path()
        if not path:
            messagebox.showinfo("No Report File", "Select a report file first.")
            return
        target = filedialog.asksaveasfilename(initialfile=path.name)
        if target:
            shutil.copyfile(path, target)
            self.log_msg(f"Saved report file as: {target}")

    def open_professional_report(self):
        path = self.report_files.get("professional_html") or self.report_files.get("generator_html")
        if (not path or not Path(path).exists()) and self.report_files.get("generator_html"):
            path = self.report_files.get("generator_html")
        if path and Path(path).exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            messagebox.showinfo("No Professional Report", "No HTML report is available for this package.")

    def export_professional_pdf(self):
        path = self.report_files.get("professional_pdf") or self.report_files.get("generator_pdf")
        if (not path or not Path(path).exists()) and self.report_files.get("generator_pdf"):
            path = self.report_files.get("generator_pdf")
        if path and Path(path).exists():
            subprocess.run(["open", str(path)], check=False)
            return

        report_dir = self.latest_report_dir
        if report_dir and Path(report_dir).exists():
            if Path(report_dir).name.startswith("generator_"):
                messagebox.showinfo("PDF Unavailable", "Generator PDF is not available. Generate the commissioning report again after installing ReportLab.")
                return
            ident = self.instrument_id if self.instrument_id != "Not connected" else "Unknown"
            report_type = "Single Screen Waveform Report" if Path(report_dir).name.startswith("single_") else "Replay Export"
            self.build_professional_report(Path(report_dir), ident, report_type)
            self.register_report_session(Path(report_dir))
            path = self.report_files.get("professional_pdf")
            if path and Path(path).exists():
                subprocess.run(["open", str(path)], check=False)
                return

        messagebox.showinfo("PDF Unavailable", "PDF generator is not available, or no professional PDF could be produced.")

    def copy_summary_to_clipboard(self):
        path = self.report_files.get("summary_metrics") or self.report_files.get("generator_settings_csv")
        if (not path or not Path(path).exists()) and self.report_files.get("generator_settings_csv"):
            path = self.report_files.get("generator_settings_csv")
        if not path or not Path(path).exists():
            messagebox.showinfo("No Summary", "No summary CSV is available for this package.")
            return
        rows = Path(path).read_text(encoding="utf-8", errors="replace")[:5000]
        self.root.clipboard_clear()
        self.root.clipboard_append(rows)
        self.log_msg(f"Copied {Path(path).name} preview to clipboard")

    def open_stitched_replay_view(self):
        path = self.report_files.get("stitched_plot") or self.report_files.get("stitched_csv")
        if path and Path(path).exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            messagebox.showinfo("No Stitched Replay View", "No stitched replay view is available for this report package.")

    def open_waterfall_replay_view(self):
        path = self.report_files.get("waterfall_plot")
        if path and Path(path).exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            messagebox.showinfo("No Waterfall View", "No waterfall replay heatmap is available for this report package.")

    def open_last_fft_plot(self):
        candidates = [
            self.report_files.get("professional_fft_plot"),
            self.report_files.get("single_fft_plot"),
        ]
        report_dir = self.latest_report_dir or self.discover_latest_report_dir()
        if report_dir:
            report_dir = Path(report_dir)
            candidates.extend([
                report_dir / "fft_spectrum.png",
                report_dir / "single_fft_plot.png",
                report_dir / "stitched_replay_overview.png",
            ])

        for path in candidates:
            if path and Path(path).exists():
                subprocess.run(["open", str(path)], check=False)
                return

        messagebox.showinfo("No FFT Plot", "No FFT plot is available yet. Generate a waveform report or compute FFT first.")

    def open_last_report(self):
        report_dir = self.latest_report_dir or self.discover_latest_report_dir()
        if report_dir and Path(report_dir).exists():
            if self.latest_report_dir != Path(report_dir):
                self.register_report_session(Path(report_dir))
            subprocess.run(["open", str(report_dir)], check=False)
        else:
            messagebox.showinfo("No Report", "No report bundle has been generated yet.")

    def load_waveform_thread(self):
        path = self.ask_waveform_path()
        if path:
            self.run_worker(lambda: self.load_waveform(path))

    def load_waveform_and_fft_thread(self):
        path = self.ask_waveform_path()
        if path:
            self.run_worker(lambda: self.load_waveform_and_fft(path))

    def ask_waveform_path(self):
        path = filedialog.askopenfilename(
            initialdir=str(self.outdir),
            title="Load raw waveform",
            filetypes=(("Raw waveform", "*.bin"), ("All files", "*.*")),
        )
        return Path(path) if path else None

    def load_waveform(self, path):
        try:
            size = path.stat().st_size
            self.last_waveform_raw = path
            self.last_raw = path
            self.ui_call(self.waveform_status.set, f"Loaded waveform: {path.name} ({size} bytes)")
            self.log_msg(f"Loaded waveform raw: {path}")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Waveform Error", exc)

    def load_waveform_and_fft(self, path):
        self.load_waveform(path)
        self.compute_fft()

    def compute_fft_thread(self):
        self.run_worker(self.compute_fft)

    def compute_fft(self):
        try:
            if not self.last_waveform_raw or not self.last_waveform_raw.exists():
                raise RuntimeError("No waveform file loaded. Download or load a raw waveform first.")

            sample_rate = float(self.sample_rate_var.get())
            if sample_rate <= 0:
                raise RuntimeError("Sample rate must be greater than zero.")

            self.set_progress(text="FFT...", indeterminate=True)
            raw = self.last_waveform_raw.read_bytes()
            result = compute_fft_from_bytes(raw, sample_rate_hz=sample_rate)
            self.last_fft = result

            dom_freq, dom_amp = result["dominant"]
            msg = (
                f"FFT from {self.last_waveform_raw.name}: "
                f"{len(result['samples'])} points, dominant {dom_freq:.4g} Hz at {dom_amp:.4g}"
            )
            self.ui_call(self.fft_status.set, msg)
            self.log_msg(msg)
            self.ui_call(self.draw_fft)
            self.set_progress(100, 100, "FFT complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("FFT Error", exc)

    def draw_fft(self):
        canvas = self.fft_canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        pad = 36

        canvas.create_rectangle(0, 0, width, height, fill="white", outline="")
        canvas.create_line(pad, height - pad, width - 12, height - pad, fill="#333333")
        canvas.create_line(pad, 12, pad, height - pad, fill="#333333")

        if not self.last_fft:
            canvas.create_text(width / 2, height / 2, text="No FFT data", fill="#555555")
            return

        freq = self.last_fft["frequency"]
        amp = self.last_fft["amplitude"]
        if len(freq) < 2 or max(amp) <= 0:
            canvas.create_text(width / 2, height / 2, text="FFT data is empty", fill="#555555")
            return

        plot_w = max(1, width - pad - 18)
        plot_h = max(1, height - pad - 18)
        max_freq = max(freq) or 1.0
        max_amp = max(amp) or 1.0
        points = []

        for f, a in zip(freq, amp):
            x = pad + (f / max_freq) * plot_w
            y = height - pad - (a / max_amp) * plot_h
            points.extend((x, y))

        if len(points) >= 4:
            canvas.create_line(*points, fill="#0b6f85", width=2)

        canvas.create_text(pad, 8, text=f"Max {max_amp:.3g}", anchor="nw", fill="#333333")
        canvas.create_text(width - 12, height - pad + 8, text=f"{max_freq:.3g} Hz", anchor="ne", fill="#333333")


def main():
    root = tk.Tk()
    FlukeScopeSuiteProV3(root)
    root.mainloop()
