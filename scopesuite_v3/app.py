import subprocess
import shutil
import sys
import threading
import time
import os
import csv
import re
import math
import json
import ctypes
import traceback
from io import BytesIO
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
    WORK_BAUD,
)
from .calibration import (
    FLUKE_REPLAY_TIMEBASE_PRESET,
    get_expected_line_frequency,
    get_timebase_correction,
    set_expected_line_frequency,
    set_timebase_correction,
)
from .fft_tools import compute_fft_from_bytes
from .fluke_connect import DEFAULT_FLUKE_CONNECT_INBOX, import_fluke_connect_folder
from .image_decoder import bottom_status_region_detected, save_screen_debug_files
from .screen_capture_modes import (
    scope_model_from_ident,
    screen_capture_filename_prefix,
    screen_capture_mode_for_ident,
)
from .serial_client import (
    FlukeSerialClient,
    abort_binary_transfer_and_close,
    available_ports,
    safe_release_scope,
)
from .autotune_engine import (
    SAFETY_WARNING,
    analyze_autotune_session,
    capture_waveform_pair,
    write_autotune_package,
)

SAFE_MODE_LABEL = "Legacy Safe ID Mode"


class FlukeScopeSuiteProV3:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        initial_w = min(1180, max(720, self.root.winfo_screenwidth() - 80))
        initial_h = min(820, max(500, self.root.winfo_screenheight() - 80))
        self.root.geometry(f"{initial_w}x{initial_h}")
        self.root.minsize(720, 500)

        self.outdir = DEFAULT_OUTPUT_DIR
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.session_root = self.outdir
        self.session_dir = None
        self.diagnostics_root = self.session_root / "_diagnostics"
        self.capture_index = 0

        self.last_png = None
        self.last_raw = None
        self.last_waveform_raw = None
        self.last_fft = None
        self.image_ref = None
        self.image_full_ref = None
        self.image_preview_path = None
        self.image_render_after_id = None
        self.report_image_refs = []
        self.latest_report_dir = None
        self.report_files = {}
        self.report_file_items = []
        self.report_file_meta = []
        self.report_preview_image_ref = None
        self.report_show_advanced_var = tk.BooleanVar(value=False)
        self.enable_capture_debug_mode_var = tk.BooleanVar(value=False)
        self.advanced_transfer_mode_var = tk.BooleanVar(value=False)
        self.report_package_title_var = tk.StringVar(value="No completed report package")
        self.report_package_detail_var = tk.StringVar(value="Run a replay export to generate an in-app report package.")
        self.report_summary_vars = {}
        self.include_latest_screen_in_reports_var = tk.BooleanVar(value=True)
        self.safe_199c_mode_var = tk.BooleanVar(value=True)
        self.active_serial = None
        self.active_client = None
        self.instrument_id = "Not connected"
        self.current_scope_baud = INITIAL_BAUD
        self.instrument_profile = {
            "model": "",
            "ident": "",
            "port": "",
            "baud": INITIAL_BAUD,
            "safe_mode": True,
            "remote_used": False,
        }
        self.worker_running = False
        self.capture_active = False
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
        self.autotune_baseline = None
        self.autotune_load_step = None
        self.autotune_analysis = None
        self.autotune_session_dir = None
        self.autotune_metric_vars = {}
        self.autotune_setting_vars = {}
        self.autotune_final_setting_vars = {}
        self.autotune_mode_widgets = []
        self.autotune_glove_mode_var = tk.BooleanVar(value=True)
        self.autotune_sunlight_mode_var = tk.BooleanVar(value=True)
        self.autotune_notes_var = tk.StringVar(value="")

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_mode = tk.StringVar(value="determinate")
        self.progress_text = tk.StringVar(value="Idle")
        self.status_var = tk.StringVar(value="Ready")
        self.connection_state_var = tk.StringVar(value="DISCONNECTED")
        self.connection_state = "DISCONNECTED"
        self.ui_scale_var = tk.StringVar(value="Tablet")
        self.field_mode_var = tk.StringVar(value="Sunlight")
        self.port_var = tk.StringVar()
        self.job_name_var = tk.StringVar(value="")
        self.customer_var = tk.StringVar(value="")
        self.site_var = tk.StringVar(value="")
        self.waveform_status = tk.StringVar(value="No waveform downloaded yet.")
        self.fft_status = tk.StringVar(value="Load or download a waveform, then compute FFT.")
        self.sample_rate_var = tk.StringVar(value="1.0")
        self.fluke_connect_inbox_var = tk.StringVar(value=str(DEFAULT_FLUKE_CONNECT_INBOX))
        self.timebase_correction_var = tk.StringVar(value=f"{get_timebase_correction():.9f}")
        self.expected_line_frequency_var = tk.StringVar(value=f"{get_expected_line_frequency():.1f}")
        self.user_settings_path = DEFAULT_OUTPUT_DIR / "scopesuite_user_settings.json"
        self.load_user_settings()

        self.build_ui()
        self.apply_tablet_style()
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)
        self.log_startup_info()
        self.refresh_ports()

    def build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text=APP_NAME, font=("Arial", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, anchor="e", font=("Arial", 11), width=34).grid(row=0, column=1, sticky="ew", padx=(10, 0))

        controls = ttk.LabelFrame(self.root, text="Connection", padding=(12, 8))
        controls.grid(row=1, column=0, sticky="ew", padx=18)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(5, weight=1)

        ttk.Label(controls, text="COM", font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=16, font=("Arial", 13))
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8), ipady=4)
        ttk.Button(controls, text="Refresh", width=9, command=self.refresh_ports).grid(row=0, column=2, sticky="ew", padx=3, ipady=4)
        ttk.Button(controls, text="Test", width=7, command=self.test_connection_thread).grid(row=0, column=3, sticky="ew", padx=3, ipady=4)
        ttk.Label(controls, text="Scale").grid(row=0, column=4, sticky="e", padx=(12, 4))
        self.ui_scale_combo = ttk.Combobox(
            controls,
            textvariable=self.ui_scale_var,
            values=("Compact", "Standard", "Tablet"),
            width=8,
            state="readonly",
            font=("Arial", 12),
        )
        self.ui_scale_combo.grid(row=0, column=5, sticky="w", padx=(0, 8), ipady=3)
        self.ui_scale_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_tablet_style())
        ttk.Radiobutton(controls, text="Light", variable=self.field_mode_var, value="Light", command=self.apply_tablet_style).grid(row=1, column=0, sticky="w", padx=3, pady=(8, 0))
        ttk.Radiobutton(controls, text="Dark", variable=self.field_mode_var, value="Dark", command=self.apply_tablet_style).grid(row=1, column=1, sticky="w", padx=3, pady=(8, 0))
        ttk.Radiobutton(controls, text="Sun", variable=self.field_mode_var, value="Sunlight", command=self.apply_tablet_style).grid(row=1, column=2, sticky="w", padx=3, pady=(8, 0))
        self.connection_tile = tk.Label(
            controls,
            textvariable=self.connection_state_var,
            bg="#6b7280",
            fg="white",
            font=("Arial", 11, "bold"),
            padx=8,
            pady=7,
            relief="solid",
            bd=1,
        )
        self.connection_tile.grid(row=1, column=3, columnspan=3, sticky="nsew", padx=(10, 0), pady=(8, 0))

        progress_frame = ttk.Frame(self.root, padding=(18, 4, 18, 0))
        progress_frame.grid(row=2, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=28, anchor="e").grid(row=0, column=1, padx=(10, 0))

        # Cap the requested notebook size so hidden, content-heavy tabs do not force
        # the whole field UI off-screen at 125-175% Windows display scaling.
        notebook_w = min(820, max(620, self.root.winfo_screenwidth() - 120))
        notebook_h = min(500, max(360, self.root.winfo_screenheight() - 260))
        self.tabs = ttk.Notebook(self.root, style="Workspace.TNotebook", width=notebook_w, height=notebook_h)
        self.tabs.grid(row=3, column=0, sticky="nsew", padx=18, pady=(8, 14))

        self.build_capture_tab()
        self.build_waveform_tab()
        self.build_autotune_tab()
        self.build_fft_tab()
        self.build_reports_tab()
        self.build_generator_reports_tab()
        self.build_settings_tab()
        self.build_log_tab()

    def load_user_settings(self):
        try:
            if not self.user_settings_path.exists():
                self.apply_calibration_settings(save=False)
                return
            data = json.loads(self.user_settings_path.read_text(encoding="utf-8"))
            if "timebase_correction" in data:
                self.timebase_correction_var.set(str(data["timebase_correction"]))
            if "expected_line_frequency_hz" in data:
                self.expected_line_frequency_var.set(str(data["expected_line_frequency_hz"]))
            if "ui_scale" in data:
                self.ui_scale_var.set(str(data["ui_scale"]))
            if "field_mode" in data:
                self.field_mode_var.set(str(data["field_mode"]))
            self.apply_calibration_settings(save=False)
        except Exception:
            self.apply_calibration_settings(save=False)

    def save_user_settings(self):
        self.apply_calibration_settings(save=False)
        data = {
            "timebase_correction": get_timebase_correction(),
            "expected_line_frequency_hz": get_expected_line_frequency(),
            "ui_scale": self.ui_scale_var.get(),
            "field_mode": self.field_mode_var.get(),
        }
        self.user_settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.log_msg(f"User settings saved: {self.user_settings_path}")

    def apply_calibration_settings(self, save=True):
        correction = set_timebase_correction(self.timebase_correction_var.get())
        expected = set_expected_line_frequency(self.expected_line_frequency_var.get())
        self.timebase_correction_var.set(f"{correction:.9f}")
        self.expected_line_frequency_var.set(f"{expected:.1f}")
        if save:
            self.save_user_settings()
        return correction, expected

    def use_fluke_replay_timebase_preset(self):
        self.timebase_correction_var.set(f"{FLUKE_REPLAY_TIMEBASE_PRESET:.9f}")
        self.expected_line_frequency_var.set("60.0")
        self.apply_calibration_settings(save=True)
        self.log_msg("Fluke replay timebase correction preset applied: 1.111111111")

    def apply_tablet_style(self):
        try:
            style = ttk.Style()
            mode = self.ui_scale_var.get() if hasattr(self, "ui_scale_var") else "Tablet"
            field_mode = self.field_mode_var.get() if hasattr(self, "field_mode_var") else "Sunlight"
            sizes = {
                "Compact": {"normal": 10, "button": 10, "tab": 12, "heading": 16, "pad": (10, 7)},
                "Standard": {"normal": 11, "button": 11, "tab": 13, "heading": 18, "pad": (12, 9)},
                "Tablet": {"normal": 12, "button": 12, "tab": 14, "heading": 20, "pad": (16, 12)},
            }[mode]
            if field_mode == "Dark":
                bg = "#1E1E1E"
                fg = "#F2F2F2"
                secondary_fg = "#D0D0D0"
                disabled_fg = "#8E8E8E"
                panel = "#252526"
                entry_bg = "#2D2D30"
                button_bg = "#2D2D30"
                button_hover = "#3E3E42"
                button_disabled = "#3A3A3A"
                disabled_border = "#555555"
                accent = "#0078D7"
                selected_tab_bg = "#0E639C"
                selected_tab_fg = "#FFFFFF"
                inactive_tab = "#2D2D30"
                inactive_tab_fg = "#D0D0D0"
                hover_tab_bg = "#3E3E42"
                hover_tab_fg = "#FFFFFF"
            elif field_mode == "Sunlight":
                bg, fg, panel, accent = "#ffffff", "#050505", "#f7f7f7", "#004c97"
                secondary_fg = fg
                disabled_fg = "#666666"
                entry_bg = "#ffffff"
                button_bg = panel
                button_hover = "#eef5ff"
                button_disabled = "#e4e4e4"
                disabled_border = "#aaaaaa"
                selected_tab_bg = "#F0F7FF"
                selected_tab_fg = "#000000"
                inactive_tab = "#D8E2ED"
                inactive_tab_fg = "#111111"
                hover_tab_bg = "#C7DDF3"
                hover_tab_fg = "#000000"
            else:
                bg, fg, panel, accent = "#f2f4f7", "#111111", "#ffffff", "#336699"
                secondary_fg = fg
                disabled_fg = "#666666"
                entry_bg = "#ffffff"
                button_bg = panel
                button_hover = "#e9f1fb"
                button_disabled = "#e4e4e4"
                disabled_border = "#aaaaaa"
                selected_tab_bg = "#E3EEFB"
                selected_tab_fg = "#000000"
                inactive_tab = "#D5DEE9"
                inactive_tab_fg = "#111111"
                hover_tab_bg = "#C9D9EC"
                hover_tab_fg = "#000000"
            try:
                if field_mode == "Dark":
                    style.theme_use("clam")
            except Exception:
                pass
            self.root.configure(bg=bg)
            self.root.option_add("*Font", ("Arial", sizes["normal"]))
            self.root.option_add("*TCombobox*Listbox.font", ("Arial", sizes["normal"]))
            self.root.option_add("*TCombobox*Listbox.background", entry_bg)
            self.root.option_add("*TCombobox*Listbox.foreground", fg)
            self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            style.configure(".", font=("Arial", sizes["normal"]), background=bg, foreground=fg)
            style.configure("TFrame", background=bg)
            style.configure("TLabelframe", background=panel, foreground=fg, padding=12, bordercolor=disabled_border)
            style.configure("TLabelframe.Label", font=("Arial", sizes["normal"], "bold"), background=panel, foreground=fg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.map("TLabel", foreground=[("disabled", disabled_fg)])
            style.configure(
                "TButton",
                font=("Arial", sizes["button"], "bold"),
                padding=sizes["pad"],
                background=button_bg,
                foreground=fg,
                bordercolor=disabled_border,
                lightcolor=button_bg,
                darkcolor=button_bg,
                focusthickness=1,
                focuscolor=accent,
            )
            style.map(
                "TButton",
                background=[
                    ("disabled", button_disabled),
                    ("pressed", accent),
                    ("active", button_hover),
                ],
                foreground=[
                    ("disabled", disabled_fg),
                    ("pressed", "#ffffff"),
                    ("active", fg),
                ],
                bordercolor=[
                    ("disabled", disabled_border),
                    ("focus", accent),
                ],
            )
            style.configure(
                "TCheckbutton",
                background=bg,
                foreground=fg,
                font=("Arial", sizes["normal"]),
                indicatorcolor=entry_bg,
            )
            style.configure(
                "TRadiobutton",
                background=bg,
                foreground=fg,
                font=("Arial", sizes["normal"]),
                indicatorcolor=entry_bg,
            )
            style.map(
                "TCheckbutton",
                background=[("active", bg), ("disabled", bg)],
                foreground=[("disabled", disabled_fg), ("active", fg)],
                indicatorcolor=[("selected", accent), ("disabled", button_disabled)],
            )
            style.map(
                "TRadiobutton",
                background=[("active", bg), ("disabled", bg)],
                foreground=[("disabled", disabled_fg), ("active", fg)],
                indicatorcolor=[("selected", accent), ("disabled", button_disabled)],
            )
            style.configure(
                "TEntry",
                fieldbackground=entry_bg,
                background=entry_bg,
                foreground=fg,
                insertcolor=fg,
                bordercolor=disabled_border,
                lightcolor=entry_bg,
                darkcolor=entry_bg,
            )
            style.map(
                "TEntry",
                fieldbackground=[("disabled", button_disabled), ("readonly", entry_bg)],
                foreground=[("disabled", disabled_fg), ("readonly", fg)],
                bordercolor=[("focus", accent), ("disabled", disabled_border)],
            )
            style.configure(
                "TCombobox",
                fieldbackground=entry_bg,
                background=button_bg,
                foreground=fg,
                arrowcolor=fg,
                bordercolor=disabled_border,
                lightcolor=entry_bg,
                darkcolor=entry_bg,
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", entry_bg), ("disabled", button_disabled)],
                background=[("active", button_hover), ("disabled", button_disabled)],
                foreground=[("disabled", disabled_fg), ("readonly", fg)],
                arrowcolor=[("disabled", disabled_fg), ("active", fg)],
                bordercolor=[("focus", accent), ("disabled", disabled_border)],
            )
            sidebar_tabs = mode == "Tablet"
            tab_position = "wn" if sidebar_tabs else "n"
            tab_padding = (16, 14) if sidebar_tabs else (18, 10)
            style.configure("TNotebook", background=bg, borderwidth=0)
            style.configure("Workspace.TNotebook", background=bg, borderwidth=0, tabposition=tab_position)
            style.configure(
                "TNotebook.Tab",
                font=("Arial", sizes["tab"], "bold"),
                padding=tab_padding,
                background=inactive_tab,
                foreground=inactive_tab_fg,
                bordercolor=disabled_border,
                lightcolor=inactive_tab,
                darkcolor=inactive_tab,
            )
            style.map(
                "TNotebook.Tab",
                background=[
                    ("selected", selected_tab_bg),
                    ("active", hover_tab_bg),
                    ("disabled", button_disabled),
                ],
                foreground=[
                    ("selected", selected_tab_fg),
                    ("active", hover_tab_fg),
                    ("disabled", disabled_fg),
                ],
                bordercolor=[("selected", accent), ("active", accent)],
                lightcolor=[("selected", accent), ("active", hover_tab_bg)],
                darkcolor=[("selected", accent), ("active", hover_tab_bg)],
            )
            style.configure("Summary.TLabel", font=("Arial", sizes["normal"] + 2, "bold"), background=bg, foreground=fg)
            style.configure("SummaryValue.TLabel", font=("Arial", sizes["normal"] + 3, "bold"), background=bg, foreground=accent)
            style.configure(
                "AutoTune.TButton",
                font=("Arial", sizes["button"] + 1, "bold"),
                padding=(sizes["pad"][0], sizes["pad"][1] + 2),
                background=button_bg,
                foreground=fg,
                bordercolor=disabled_border,
            )
            style.map(
                "AutoTune.TButton",
                background=[("disabled", button_disabled), ("pressed", accent), ("active", button_hover)],
                foreground=[("disabled", disabled_fg), ("pressed", "#ffffff"), ("active", fg)],
            )
            for widget_name in ("waveform_text", "report_text", "log", "settings_text", "generator_notes", "autotune_notes", "autotune_conditions_text"):
                widget = getattr(self, widget_name, None)
                if widget is not None:
                    try:
                        widget.configure(
                            bg=entry_bg,
                            fg=fg,
                            insertbackground=fg,
                            selectbackground=accent,
                            selectforeground="#ffffff",
                            font=("Consolas" if "text" in widget_name or widget_name == "log" else "Arial", sizes["normal"]),
                        )
                    except Exception:
                        pass
            if hasattr(self, "report_listbox"):
                self.report_listbox.configure(
                    bg=entry_bg,
                    fg=fg,
                    selectbackground=accent,
                    selectforeground="#ffffff",
                    font=("Consolas", sizes["normal"]),
                )
            for canvas_name in ("image_canvas", "report_canvas", "fft_canvas"):
                canvas = getattr(self, canvas_name, None)
                if canvas is not None:
                    canvas.configure(bg="#ffffff" if field_mode != "Dark" else "#101316")
            self.update_connection_tile()
        except Exception:
            pass

    def set_connection_state(self, state):
        self.ui_call(self._set_connection_state, state)

    def _set_connection_state(self, state):
        self.connection_state = state
        self.connection_state_var.set(state)
        self.update_connection_tile()

    def update_connection_tile(self):
        if not hasattr(self, "connection_tile"):
            return
        colors = {
            "DISCONNECTED": ("#6b7280", "white"),
            "CONNECTED": ("#087f5b", "white"),
            "TRANSFERRING": ("#0b5cab", "white"),
            "WARNING": ("#f59f00", "black"),
            "METER LOCKED / RECOVERY NEEDED": ("#d9480f", "white"),
            "ERROR": ("#b00020", "white"),
        }
        bg, fg = colors.get(self.connection_state_var.get(), colors["DISCONNECTED"])
        self.connection_tile.configure(bg=bg, fg=fg)

    def update_debug_capture_visibility(self):
        button = getattr(self, "debug_capture_button", None)
        if button is None:
            return
        frame = getattr(self, "capture_replay_actions", None)
        if self.enable_capture_debug_mode_var.get():
            if frame is not None:
                frame.grid()
            button.grid()
        else:
            button.grid_remove()
            if frame is not None:
                frame.grid_remove()

    def build_capture_tab(self):
        tab = ttk.Frame(self.tabs, padding=16)
        self.capture_tab = tab
        tab.columnconfigure(0, weight=0, minsize=285)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)
        self.tabs.add(tab, text="Screen Capture")

        action_area = ttk.Frame(tab)
        self.capture_action_area = action_area
        action_area.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        action_area.columnconfigure(0, weight=1)
        action_area.rowconfigure(0, weight=0)
        action_area.rowconfigure(1, weight=0)
        action_area.rowconfigure(2, weight=1)

        live = ttk.LabelFrame(action_area, text="Live Scope Screen", padding=12)
        self.capture_live_actions = live
        live.grid(row=0, column=0, sticky="ew")
        live.columnconfigure(0, weight=1)
        for row, (text, command) in enumerate([
            ("Capture Screen", self.capture_screen_thread),
            ("Load Saved Capture", self.replay_capture_thread),
            ("Open Last Screen Image", self.open_last_image),
            ("Copy Latest Screen to Clipboard", self.copy_latest_screen_to_clipboard),
            ("New Session", self.new_session),
            ("Open Output Folder", self.open_folder),
        ]):
            ttk.Button(live, text=text, command=command).grid(row=row, column=0, sticky="ew", padx=5, pady=4, ipady=5)

        replay = ttk.LabelFrame(action_area, text="Debug Capture Files", padding=12)
        self.capture_replay_actions = replay
        replay.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        replay.columnconfigure(0, weight=1)
        self.debug_capture_button = ttk.Button(replay, text="Load Capture (Debug Mode)", command=self.replay_debug_capture_thread)
        self.debug_capture_button.grid(row=0, column=0, sticky="ew", padx=5, pady=5, ipady=6)
        self.update_debug_capture_visibility()

        image_frame = ttk.LabelFrame(tab, text="Scope Screen Preview", padding=8)
        self.capture_image_frame = image_frame
        image_frame.grid(row=0, column=1, sticky="nsew")
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1, minsize=300)
        self.image_canvas = tk.Canvas(image_frame, bg="white", relief="sunken", highlightthickness=0)
        self.image_canvas.grid(row=0, column=0, sticky="nsew")
        image_vscroll = ttk.Scrollbar(image_frame, orient="vertical", command=self.image_canvas.yview)
        image_vscroll.grid(row=0, column=1, sticky="ns")
        image_hscroll = ttk.Scrollbar(image_frame, orient="horizontal", command=self.image_canvas.xview)
        image_hscroll.grid(row=1, column=0, sticky="ew")
        self.image_canvas.configure(
            xscrollcommand=image_hscroll.set,
            yscrollcommand=image_vscroll.set,
        )
        self.image_canvas.bind("<Configure>", self.on_screen_preview_resize)

    def build_waveform_tab(self):
        tab = ttk.Frame(self.tabs, padding=16)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)
        self.tabs.add(tab, text="Waveform")

        single = ttk.LabelFrame(tab, text="Single Capture and Report", padding=12)
        single.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for col in range(4):
            single.columnconfigure(col, weight=1)
        for col, (text, command) in enumerate([
            ("Download Channel A Raw", lambda: self.waveform_thread("10")),
            ("Download Channel B Raw", lambda: self.waveform_thread("20")),
            ("Download Both Raw", self.waveform_both_thread),
            ("Single Waveform Report", self.single_waveform_report_thread),
            ("Live Waveform Analysis", self.live_waveform_analysis_thread),
            ("Image-Only Screen Report", self.analyze_this_screen_thread),
        ]):
            ttk.Button(single, text=text, command=command).grid(row=col // 3, column=col % 3, sticky="ew", padx=5, pady=5, ipady=6)
        ttk.Checkbutton(
            single,
            text="Include latest screen capture in waveform reports",
            variable=self.include_latest_screen_in_reports_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(8, 0))

        replay = ttk.LabelFrame(tab, text="Replay Memory Export", padding=12)
        replay.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for col in range(3):
            replay.columnconfigure(col, weight=1)
        for col, (text, command) in enumerate([
            ("Export Replay Set", self.export_replay_thread),
            ("Analyze Full Capture (Deep Memory)", self.analyze_full_capture_thread),
            ("Stitched Replay View / Report", self.open_stitched_replay_view),
            ("Waterfall / Heatmap View", self.open_waterfall_replay_view),
        ]):
            ttk.Button(replay, text=text, command=command).grid(row=col // 2, column=col % 2, sticky="ew", padx=5, pady=5, ipady=6)

        offline = ttk.LabelFrame(tab, text="Offline Files", padding=12)
        offline.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        offline.columnconfigure(0, weight=1)
        offline.columnconfigure(1, weight=1)
        offline.columnconfigure(2, weight=1)
        ttk.Button(offline, text="Load Raw Waveform", command=self.load_waveform_thread).grid(row=0, column=0, sticky="ew", padx=5, pady=5, ipady=6)
        ttk.Button(offline, text="Load Raw and FFT", command=self.load_waveform_and_fft_thread).grid(row=0, column=1, sticky="ew", padx=5, pady=5, ipady=6)
        ttk.Button(offline, text="Import Fluke Connect Inbox", command=self.import_fluke_connect_thread).grid(row=0, column=2, sticky="ew", padx=5, pady=5, ipady=6)
        ttk.Label(offline, text="Fluke Connect folder").grid(row=1, column=0, sticky="w", padx=5, pady=(6, 0))
        ttk.Entry(offline, textvariable=self.fluke_connect_inbox_var).grid(row=1, column=1, sticky="ew", padx=5, pady=(6, 0), ipady=3)
        ttk.Button(offline, text="Choose Fluke Connect Folder", command=self.choose_fluke_connect_folder).grid(row=1, column=2, sticky="ew", padx=5, pady=(6, 0), ipady=3)

        ttk.Label(tab, textvariable=self.waveform_status).grid(row=3, column=0, sticky="w", pady=(0, 8))

        self.waveform_text = tk.Text(tab, height=12, wrap="word", font=("Consolas", 12))
        self.waveform_text.grid(row=4, column=0, sticky="nsew")
        self.waveform_text.insert(
            "end",
            "Channel A = QW 10\n"
            "Channel B = QW 20\n\n"
            "Export Replay Set reads RP status, walks every replay frame in deep memory, "
            "and saves both channels for each frame.\n",
        )
        self.waveform_text.configure(state="disabled")

    def build_autotune_tab(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.autotune_tab = tab
        tab.columnconfigure(0, weight=0, minsize=310)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        self.tabs.add(tab, text="AutoTune / Generator")

        style = ttk.Style()
        style.configure("AutoTune.TButton", font=("Arial", 14, "bold"), padding=(10, 14))
        style.configure("AutoTune.Tile.TLabel", font=("Arial", 15, "bold"), padding=(10, 8))
        style.configure("AutoTune.Value.TLabel", font=("Arial", 18, "bold"), padding=(10, 6))

        warning = tk.Label(
            tab,
            text=SAFETY_WARNING,
            bg="#fff3cd",
            fg="#7a4b00",
            font=("Arial", 13, "bold"),
            anchor="w",
            padx=12,
            pady=10,
            wraplength=1120,
        )
        warning.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        modes = ttk.Frame(tab)
        modes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(modes, text="High Contrast / Sunlight Mode", variable=self.autotune_sunlight_mode_var, command=self.apply_autotune_modes).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(modes, text="Glove Mode", variable=self.autotune_glove_mode_var, command=self.apply_autotune_modes).pack(side="left", padx=(0, 18))
        ttk.Label(modes, text="Offline rule-based advisor. Recommendations only.").pack(side="left")

        workflow = ttk.LabelFrame(tab, text="One-Screen Workflow", padding=10)
        workflow.grid(row=2, column=0, rowspan=2, sticky="nsew", padx=(0, 10))
        workflow.columnconfigure(0, weight=1)
        actions = [
            ("1. Connect Scope", self.autotune_connect_scope_thread),
            ("2. Capture No-Load Baseline", self.autotune_capture_baseline_thread),
            ("3. Capture Load-Step Event", self.autotune_capture_load_step_thread),
            ("4. Analyze Response", self.autotune_analyze_thread),
            ("5. Show Recommendation", self.autotune_show_recommendation),
            ("6. Save Final Settings", self.autotune_save_final_settings),
            ("7. Generate Commissioning Report", self.autotune_generate_report_thread),
        ]
        for row, (text, command) in enumerate(actions):
            button = ttk.Button(workflow, text=text, command=command, style="AutoTune.TButton")
            button.grid(row=row, column=0, sticky="ew", pady=5, ipady=4)
            self.autotune_mode_widgets.append(button)

        metrics = ttk.LabelFrame(tab, text="Live Metric Tiles", padding=10)
        metrics.grid(row=2, column=1, sticky="ew")
        for col in range(4):
            metrics.columnconfigure(col, weight=1)
        metric_specs = [
            ("voltage", "Voltage", "-- V"),
            ("frequency", "Frequency", "-- Hz"),
            ("current", "Current", "-- A"),
            ("power_factor", "Power Factor", "--"),
            ("thd_v", "THD-V", "-- %"),
            ("thd_i", "THD-I", "-- %"),
            ("stability", "Stability Status", "WAIT"),
        ]
        for idx, (key, label, default) in enumerate(metric_specs):
            frame = tk.Frame(metrics, bg="#111111", bd=1, relief="solid")
            frame.grid(row=idx // 4, column=idx % 4, sticky="nsew", padx=5, pady=5)
            tk.Label(frame, text=label, bg="#111111", fg="#ffffff", font=("Arial", 12, "bold")).pack(fill="x", pady=(8, 0))
            var = tk.StringVar(value=default)
            self.autotune_metric_vars[key] = var
            tk.Label(frame, textvariable=var, bg="#111111", fg="#00ff66", font=("Arial", 18, "bold")).pack(fill="x", pady=(0, 8))

        main = ttk.Frame(tab)
        main.grid(row=3, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        settings = ttk.LabelFrame(main, text="Before / Final GOV and REG Settings", padding=10)
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(10, 8))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(2, weight=1)
        ttk.Label(settings, text="Before").grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(settings, text="Final").grid(row=0, column=2, sticky="ew", padx=4)
        fields = [
            ("gov_gain_percent", "GOV Gain %"),
            ("gov_integral_percent", "GOV Integral %"),
            ("gov_ramp_sec", "GOV Ramp sec"),
            ("reg_gain_percent", "REG Gain %"),
            ("reg_integral_percent", "REG Integral %"),
            ("reg_vhz", "REG V/Hz"),
        ]
        for row, (key, label) in enumerate(fields, start=1):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", pady=4)
            before = tk.StringVar()
            final = tk.StringVar()
            self.autotune_setting_vars[key] = before
            self.autotune_final_setting_vars[key] = final
            ttk.Entry(settings, textvariable=before, font=("Arial", 13), width=12).grid(row=row, column=1, sticky="ew", padx=4, pady=4, ipady=5)
            ttk.Entry(settings, textvariable=final, font=("Arial", 13), width=12).grid(row=row, column=2, sticky="ew", padx=4, pady=4, ipady=5)

        detected = ttk.LabelFrame(main, text="Detected Conditions", padding=10)
        detected.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.autotune_conditions_text = tk.Text(detected, height=10, wrap="word", font=("Arial", 13))
        self.autotune_conditions_text.pack(fill="both", expand=True)
        self.autotune_conditions_text.insert("end", "Capture baseline and load-step waveforms, then analyze response.\n")
        self.autotune_conditions_text.configure(state="disabled")

        notes_frame = ttk.LabelFrame(main, text="Technician Notes / Recommendation", padding=10)
        notes_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)
        self.autotune_notes = tk.Text(notes_frame, height=9, wrap="word", font=("Arial", 13))
        self.autotune_notes.grid(row=0, column=0, sticky="nsew")
        self.autotune_notes.insert("end", SAFETY_WARNING + "\n\n")
        self.apply_autotune_modes()

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
        tab = ttk.Frame(self.tabs, padding=16)
        self.reports_tab = tab
        tab.columnconfigure(0, weight=0, minsize=280)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(4, weight=1)
        self.tabs.add(tab, text="Analyzer")

        package = ttk.LabelFrame(tab, text="Completed Report Package", padding=12)
        package.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        package.columnconfigure(0, weight=1)
        ttk.Label(package, textvariable=self.report_package_title_var, font=("Arial", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(package, textvariable=self.report_package_detail_var).grid(row=1, column=0, sticky="ew", pady=(3, 0))

        summary = ttk.LabelFrame(tab, text="Executive Summary", padding=12)
        summary.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        for col in range(5):
            summary.columnconfigure(col, weight=1)
        for idx, (key, label) in enumerate([
            ("vrms_v", "Vrms"),
            ("irms_a", "Irms"),
            ("frequency_hz", "Hz"),
            ("thd", "THD"),
            ("power_factor", "PF"),
        ]):
            var = tk.StringVar(value="--")
            self.report_summary_vars[key] = var
            ttk.Label(summary, text=label, style="Summary.TLabel").grid(row=0, column=idx, sticky="w", padx=8)
            ttk.Label(summary, textvariable=var, style="SummaryValue.TLabel").grid(row=1, column=idx, sticky="w", padx=8)

        actions = ttk.LabelFrame(tab, text="Analyzer Actions", padding=12)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        for col in range(6):
            actions.columnconfigure(col, weight=1)
        action_specs = [
            ("Choose Report Folder", self.choose_report_folder),
            ("Open Last Report", self.open_last_report),
            ("Refresh", self.refresh_reports_tab),
            ("Open Reports Folder", self.export_report_folder_external),
            ("Open In App", self.open_selected_report_in_app),
            ("Open External", self.open_selected_report_external),
            ("Save As", self.save_selected_report_as),
            ("Large Preview", self.open_large_preview),
            ("Copy Summary", self.copy_summary_to_clipboard),
            ("Copy HTML", self.copy_html_to_clipboard),
            ("Generate PDF", self.export_professional_pdf),
            ("Print", self.print_selected_report),
        ]
        for idx, (text, command) in enumerate(action_specs):
            ttk.Button(actions, text=text, command=command).grid(row=idx // 6, column=idx % 6, sticky="ew", padx=5, pady=5, ipady=5)

        self.report_status_var = tk.StringVar(value="No report session registered yet.")
        ttk.Label(tab, textvariable=self.report_status_var).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        left = ttk.Frame(tab)
        left.grid(row=4, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        ttk.Label(left, text="Report Files", font=("Arial", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(left, text="Advanced raw/debug files", variable=self.report_show_advanced_var, command=self.refresh_reports_tab).grid(row=1, column=0, sticky="w", pady=(6, 10))
        self.report_listbox = tk.Listbox(left, height=20, font=("Consolas", 12))
        self.report_listbox.grid(row=2, column=0, sticky="nsew")
        self.report_listbox.bind("<<ListboxSelect>>", lambda _event: self.open_selected_report_in_app())
        list_scroll = ttk.Scrollbar(left, orient="vertical", command=self.report_listbox.yview)
        list_scroll.grid(row=2, column=1, sticky="ns")
        self.report_listbox.configure(yscrollcommand=list_scroll.set)

        right = ttk.Frame(tab)
        right.grid(row=4, column=1, sticky="nsew", padx=(12, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.report_preview_title_var = tk.StringVar(value="Select a report file to preview inside the analyzer.")
        ttk.Label(right, textvariable=self.report_preview_title_var, font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.report_preview_stack = ttk.Frame(right)
        self.report_preview_stack.grid(row=1, column=0, sticky="nsew")
        self.report_preview_stack.columnconfigure(0, weight=1)
        self.report_preview_stack.rowconfigure(0, weight=1)

        self.report_text = tk.Text(self.report_preview_stack, height=24, wrap="word", font=("Consolas", 12))
        self.report_text.grid(row=0, column=0, sticky="nsew")
        self.report_text.configure(state="disabled")

        self.report_canvas = tk.Canvas(self.report_preview_stack, bg="white", relief="sunken", highlightthickness=0)
        self.report_canvas.grid(row=0, column=0, sticky="nsew")
        report_canvas_vscroll = ttk.Scrollbar(self.report_preview_stack, orient="vertical", command=self.report_canvas.yview)
        report_canvas_vscroll.grid(row=0, column=1, sticky="ns")
        report_canvas_hscroll = ttk.Scrollbar(self.report_preview_stack, orient="horizontal", command=self.report_canvas.xview)
        report_canvas_hscroll.grid(row=1, column=0, sticky="ew")
        self.report_canvas.configure(
            xscrollcommand=report_canvas_hscroll.set,
            yscrollcommand=report_canvas_vscroll.set,
        )
        self.report_canvas.bind("<Configure>", lambda _event: self.preview_report_file(self.selected_report_path(), preserve_mode=True))

        self.report_text.tkraise()

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
            "Frequency stability",
            "Voltage stability",
            "Current distortion",
            "Load step response",
            "AVR hunting signs",
            "Governor instability signs",
            "Neutral current risk",
            "Nonlinear load warning",
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

        calibration = ttk.LabelFrame(tab, text="Calibration", padding=8)
        calibration.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for col in range(6):
            calibration.columnconfigure(col, weight=1 if col in (1, 3) else 0)
        ttk.Label(calibration, text="Fluke replay timebase correction").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(calibration, textvariable=self.timebase_correction_var, width=14).grid(row=0, column=1, sticky="ew", padx=(0, 10), ipady=4)
        ttk.Label(calibration, text="Expected line Hz").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Combobox(
            calibration,
            textvariable=self.expected_line_frequency_var,
            values=("60.0", "50.0", "0.0"),
            width=8,
            state="readonly",
        ).grid(row=0, column=3, sticky="ew", padx=(0, 10), ipady=4)
        ttk.Button(calibration, text="Field Preset 1.111111111", command=self.use_fluke_replay_timebase_preset).grid(row=0, column=4, sticky="ew", padx=(0, 6))
        ttk.Button(calibration, text="Save Calibration", command=self.save_user_settings).grid(row=0, column=5, sticky="ew")

        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            actions,
            text=f"Conservative 1200 baud / {SAFE_MODE_LABEL}",
            variable=self.safe_199c_mode_var,
            command=self.update_settings_display,
        ).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(
            actions,
            text="Enable Debug Mode",
            variable=self.enable_capture_debug_mode_var,
            command=self.update_debug_capture_visibility,
        ).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(
            actions,
            text="Advanced Transfer Mode",
            variable=self.advanced_transfer_mode_var,
            command=self.update_settings_display,
        ).pack(side="left", padx=(0, 10))
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
        try:
            if self.session_dir is not None:
                with self.session_log_path().open("a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass
        status_msg = str(msg)
        if len(status_msg) > 72:
            status_msg = status_msg[:69] + "..."
        self.status_var.set(status_msg)
        if str(msg).startswith("ERROR"):
            self._set_connection_state("ERROR")

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

    def update_instrument_profile(self, ident=None, port=None, baud=None, safe_mode=None, remote_used=None):
        ident = ident if ident is not None else self.instrument_id
        port = port if port is not None else self.port_var.get().strip()
        baud = baud if baud is not None else self.current_scope_baud
        profile = dict(self.instrument_profile)
        if ident:
            profile["ident"] = ident
            profile["model"] = scope_model_from_ident(ident)
        if port:
            profile["port"] = port
        if baud:
            profile["baud"] = int(baud)
        if safe_mode is not None:
            profile["safe_mode"] = bool(safe_mode)
        if remote_used is not None:
            profile["remote_used"] = bool(remote_used)
        self.instrument_profile = profile
        return profile

    def active_profile_log_text(self, profile=None):
        profile = profile or self.instrument_profile
        model = profile.get("model") or scope_model_from_ident(profile.get("ident", "")) or "19X"
        port = profile.get("port") or self.port_var.get().strip() or "None selected"
        baud = profile.get("baud") or self.current_scope_baud or INITIAL_BAUD
        mode = "safe mode" if profile.get("safe_mode") else "normal mode"
        return f"FLUKE {model} at {port} / {baud} baud / {mode}"

    def release_serial_session(self, ser=None, client=None):
        ser = ser or self.active_serial
        client = client or self.active_client
        if ser is None or client is None:
            self.log_msg("Release failed, port already closed")
            return
        safe_release_scope(ser, client, logger=self.log_msg)
        self.log_msg("Port released")
        self.log_msg("Port closed")
        if ser is self.active_serial:
            self.active_serial = None
            self.active_client = None
        self.set_connection_state("DISCONNECTED")

    def close_serial_session_only(self, ser=None, label="Serial session"):
        ser = ser or self.active_serial
        if ser is None:
            self.log_msg(f"{label}: port already closed")
            return
        try:
            ser.flush()
        except Exception:
            pass
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        try:
            ser.close()
            self.log_msg(f"{label}: serial port closed without GL")
        except Exception as exc:
            self.log_msg(f"{label}: close failed: {exc}")
        if ser is self.active_serial:
            self.active_serial = None
            self.active_client = None
        self.set_connection_state("DISCONNECTED")

    def cleanup_dirty_binary_session(self, ser=None):
        ser = ser or self.active_serial
        abort_binary_transfer_and_close(ser, logger=self.log_msg)
        if ser is self.active_serial:
            self.active_serial = None
            self.active_client = None
        self.set_connection_state("DISCONNECTED")

    def release_meter(self):
        self.log_msg("Manual Release Meter requested by user")
        self.release_serial_session(self.active_serial, self.active_client)

    def force_disconnect(self):
        ser = self.active_serial
        self.active_serial = None
        self.active_client = None
        if ser is not None:
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            try:
                ser.close()
                self.log_msg("Force Disconnect: serial port closed")
            except Exception as exc:
                self.log_msg(f"Force Disconnect close failed: {exc}")
        self.set_connection_state("DISCONNECTED")

    def on_app_close(self):
        if self.active_serial is not None and self.active_client is not None:
            self.release_serial_session(self.active_serial, self.active_client)
        self.root.destroy()

    def run_worker(self, target):
        if self.worker_running:
            messagebox.showinfo("Busy", "A capture or transfer is already running.")
            return

        self.worker_running = True
        self.set_connection_state("TRANSFERRING")

        def wrapped():
            try:
                target()
            except Exception as exc:
                self.log_msg(f"ERROR: Unhandled worker failure: {exc}")
                self.log_msg(traceback.format_exc())
                self.show_error("Unexpected Error", exc)
            finally:
                self.worker_running = False
                def finish_worker():
                    if self.connection_state == "TRANSFERRING":
                        self._set_connection_state("DISCONNECTED")
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(0.0)
                    self.progress_text.set("Idle")
                self.ui_call(finish_worker)

        threading.Thread(target=wrapped, daemon=True).start()

    def clean_token(self, value, fallback="JOB"):
        value = (value or "").strip()
        if not value:
            value = fallback
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:48] or fallback

    def new_session(self, announce=True):
        model = self.clean_token(scope_model_from_ident(self.instrument_id), "SESSION")
        if model.upper() in {"NOT_CONNECTED", "NOT", "NONE"}:
            model = "SESSION"
        job = self.clean_token(self.job_name_var.get(), "")
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        base_name = f"{ts}_{model}" if not job else f"{ts}_{model}_{job}"
        self.session_dir = self.unique_child_dir(self.session_root, base_name)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.outdir = self.session_dir
        self.capture_index = 0
        self.last_png = None
        self.last_raw = None
        if announce:
            self.log_msg(f"New session folder: {self.session_dir}")
            self.update_settings_display()

    def ensure_session(self, announce=False, reason=""):
        outdir = Path(self.outdir)
        if self.session_dir is None and outdir != Path(self.session_root) and outdir.exists():
            self.session_dir = outdir
        elif self.session_dir is not None and outdir != Path(self.session_dir) and outdir != Path(self.session_root) and outdir.exists():
            self.session_dir = outdir

        if self.session_dir is None or Path(self.outdir) == Path(self.session_root):
            self.new_session(announce=announce)
            if reason:
                self.log_msg(f"Session folder created for {reason}: {self.session_dir}")
        return Path(self.session_dir)

    def diagnostics_dir(self, reason="diagnostics"):
        token = self.clean_token(reason, "diagnostics").lower()
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = self.unique_child_dir(self.diagnostics_root, f"{ts}_{token}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def unique_child_dir(self, parent, base_name):
        parent = Path(parent)
        candidate = parent / base_name
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = parent / f"{base_name}_{suffix:02d}"
        return candidate

    def session_log_path(self):
        if self.session_dir is None:
            return self.diagnostics_root / "startup_runtime_log.txt"
        return Path(self.session_dir) / "session_log.txt"

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
        folder = filedialog.askdirectory(initialdir=str(self.session_root))
        if folder:
            self.session_root = Path(folder)
            self.session_root.mkdir(parents=True, exist_ok=True)
            self.diagnostics_root = self.session_root / "_diagnostics"
            self.session_dir = None
            self.outdir = self.session_root
            self.log_msg(f"Save root changed; active session: {self.outdir}")
            self.update_settings_display()

    def open_folder(self):
        self.open_path_external(self.outdir)

    def open_path_external(self, path):
        try:
            path = Path(path)
            if not path.exists():
                raise FileNotFoundError(f"Path does not exist: {path}")
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as exc:
            self.log_msg(f"Open failed: {exc}")
            self.show_error("Open Error", exc)

    def open_path(self, path):
        self.open_path_external(path)

    def update_settings_display(self):
        port = self.port_var.get().strip() or "None selected"
        txt = (
            f"{APP_NAME}\n\n"
            f"Instrument: {self.instrument_id}\n"
            f"Instrument Profile: {self.active_profile_log_text()}\n"
            f"Serial Port: {port}\n"
            f"Current Baud: {self.current_scope_baud}\n"
            f"Conservative 1200 baud / {SAFE_MODE_LABEL}: {self.safe_199c_mode_var.get()}\n"
            f"Advanced Transfer Mode: {self.advanced_transfer_mode_var.get()}\n"
            f"Serial Settings: 8 data bits, no parity, 1 stop bit\n"
            f"Flow Control: {'disabled in Legacy Safe ID Mode capture' if self.safe_199c_mode_var.get() else 'XON/XOFF enabled for screen capture'}\n"
            f"Session Root: {self.session_root}\n"
            f"Save Folder: {self.outdir}\n\n"
            "Waveform Scaling:\n"
            f"Current Probe Scaling: {CURRENT_SCALE_A_PER_V} A/V\n"
            f"Fluke replay timebase correction: {get_timebase_correction():.9f}\n"
            f"Expected line frequency: {get_expected_line_frequency():.1f} Hz\n"
            "Calibration check: if expected line frequency is 60 Hz and measured frequency is 65-68 Hz, "
            "apply correction 1.111111111.\n\n"
            "Report Options:\n"
            "Professional HTML report: enabled\n"
            "Professional PDF report: enabled when ReportLab is available\n"
            f"Include latest screen capture in waveform reports: {self.include_latest_screen_in_reports_var.get()}\n"
            f"Customer: {self.customer_var.get()}\n"
            f"Job / Site: {self.job_name_var.get()} / {self.site_var.get()}\n"
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

    def apply_autotune_modes(self):
        glove = self.autotune_glove_mode_var.get()
        sunlight = self.autotune_sunlight_mode_var.get()
        font_size = 15 if glove else 13
        bg = "#fffdf0" if sunlight else "#f5f5f5"
        try:
            style = ttk.Style()
            style.configure("AutoTune.TButton", font=("Arial", font_size, "bold"), padding=(12, 16 if glove else 12))
            if hasattr(self, "autotune_tab"):
                self.autotune_tab.configure(style="AutoTune.TFrame")
                style.configure("AutoTune.TFrame", background=bg)
        except Exception:
            pass
        self.log_msg(f"AutoTune tablet mode updated: glove={glove}, sunlight={sunlight}")

    def autotune_set_text(self, widget, text):
        def update():
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", text)
            widget.configure(state="disabled")

        self.ui_call(update)

    def autotune_append_notes(self, text):
        def update():
            self.autotune_notes.insert("end", text)
            self.autotune_notes.see("end")

        self.ui_call(update)

    def autotune_session_folder(self):
        if self.autotune_session_dir is None:
            self.ensure_session(reason="autotune workflow")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            self.autotune_session_dir = self.outdir / "reports" / f"autotune_{ts}"
            self.autotune_session_dir.mkdir(parents=True, exist_ok=True)
        return self.autotune_session_dir

    def autotune_connect_scope_thread(self):
        self.run_worker(self.autotune_connect_scope)

    def autotune_connect_scope(self):
        ser = None
        client = None
        try:
            self.log_msg("AutoTune: connecting ScopeMeter")
            self.set_progress(text="AutoTune connect...", indeterminate=True)
            ser, client, ident = self.connect_replay_serial()
            self.instrument_id = ident
            self.log_msg(f"AutoTune connected: {ident}")
            self.ui_call(self.update_settings_display)
            self.autotune_append_notes(f"Connected ScopeMeter: {ident}\n")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("AutoTune Connect Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def autotune_capture_baseline_thread(self):
        self.run_worker(lambda: self.autotune_capture_stage("baseline"))

    def autotune_capture_load_step_thread(self):
        self.run_worker(lambda: self.autotune_capture_stage("load_step"))

    def autotune_capture_stage(self, stage):
        ser = None
        client = None
        label = "No-Load Baseline" if stage == "baseline" else "Load-Step Event"
        try:
            self.apply_calibration_settings(save=False)
            export_dir = self.autotune_session_folder()
            self.log_msg(f"AutoTune: capturing {label}")
            self.set_progress(text=f"Capturing {label}...", indeterminate=True)
            ser, client, ident = self.connect_replay_serial()
            self.instrument_id = ident
            capture = capture_waveform_pair(ser, client, export_dir, stage, log=self.log_msg)
            if stage == "baseline":
                self.autotune_baseline = capture
            else:
                self.autotune_load_step = capture
            self.autotune_append_notes(f"Captured {label}: {capture['csv_path']}\n")
            self.log_msg(f"AutoTune {label} saved: {capture['csv_path']}")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error(f"AutoTune {label} Error", exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)

    def autotune_analyze_thread(self):
        self.run_worker(self.autotune_analyze_response)

    def autotune_analyze_response(self):
        try:
            self.apply_calibration_settings(save=False)
            if self.autotune_baseline is None:
                raise RuntimeError("Capture No-Load Baseline first.")
            if self.autotune_load_step is None:
                raise RuntimeError("Capture Load-Step Event first.")
            self.log_msg("AutoTune: analyzing response with offline rule engine")
            self.set_progress(text="AutoTune analyze...", indeterminate=True)
            analysis = analyze_autotune_session(self.autotune_baseline, self.autotune_load_step)
            self.autotune_analysis = analysis
            self.ui_call(self.autotune_update_metric_tiles, analysis)
            self.ui_call(self.autotune_update_conditions, analysis)
            self.autotune_append_notes("\nAnalysis complete. Use Show Recommendation to review tuning advice.\n")
            self.log_msg(f"AutoTune analysis complete: {analysis['pass_fail']}")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("AutoTune Analysis Error", exc)

    def autotune_update_metric_tiles(self, analysis):
        load = analysis["load"]
        self.autotune_metric_vars["voltage"].set(f"{load['voltage_v']:.1f} V")
        self.autotune_metric_vars["frequency"].set(f"{load['frequency_hz']:.2f} Hz")
        self.autotune_metric_vars["current"].set(f"{load['current_a']:.2f} A")
        self.autotune_metric_vars["power_factor"].set(f"{load['power_factor']:.3f}")
        self.autotune_metric_vars["thd_v"].set(f"{load['thd_v_percent']:.2f} %")
        self.autotune_metric_vars["thd_i"].set(f"{load['thd_i_percent']:.2f} %")
        self.autotune_metric_vars["stability"].set(analysis["pass_fail"])

    def autotune_update_conditions(self, analysis):
        cond = analysis["conditions"]
        lines = [
            f"Voltage dip: {cond['voltage_dip_percent']:.2f} %",
            f"Frequency dip: {cond['frequency_dip_hz']:.2f} Hz",
            f"Voltage overshoot: {cond['voltage_overshoot_percent']:.2f} %",
            f"Frequency overshoot: {cond['frequency_overshoot_hz']:.2f} Hz",
            f"Recovery time: {cond['recovery_time_sec']:.2f} sec",
            f"Governor hunting: {'YES' if cond['governor_hunting'] else 'NO'}",
            f"AVR oscillation: {'YES' if cond['avr_oscillation'] else 'NO'}",
            f"Slow voltage recovery: {'YES' if cond['slow_voltage_recovery'] else 'NO'}",
            f"Slow frequency recovery: {'YES' if cond['slow_frequency_recovery'] else 'NO'}",
            f"Excessive THD: {'YES' if cond['excessive_thd'] else 'NO'}",
            f"Unstable power factor: {'YES' if cond['unstable_power_factor'] else 'NO'}",
        ]
        self.autotune_set_text(self.autotune_conditions_text, "\n".join(lines))

    def autotune_show_recommendation(self):
        if not self.autotune_analysis:
            messagebox.showinfo("AutoTune", "Run Analyze Response first.")
            return
        lines = [SAFETY_WARNING, "", f"Pass / Fail Summary: {self.autotune_analysis['pass_fail']}", "", "Recommendations:"]
        for action, reason in self.autotune_analysis["recommendations"]:
            lines.append(f"- {action}: {reason}")
        lines.append("")
        self.autotune_append_notes("\n".join(lines) + "\n")
        messagebox.showinfo("AutoTune Recommendation", "\n".join(lines))

    def autotune_settings_payload(self):
        payload = {}
        for key, before_var in self.autotune_setting_vars.items():
            payload[key] = {
                "before": before_var.get().strip(),
                "after": self.autotune_final_setting_vars[key].get().strip(),
            }
        return payload

    def autotune_save_final_settings(self):
        confirmed = messagebox.askyesno(
            "Confirm Manual Settings Record",
            "Record these final settings?\n\n"
            "AutoTune does not write generator controller settings. "
            "Confirm these values were entered manually by qualified personnel.",
        )
        if not confirmed:
            return
        export_dir = self.autotune_session_folder()
        settings = self.autotune_settings_payload()
        target = export_dir / "before_after_gov_reg_settings.json"
        import json
        target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        self.autotune_append_notes(f"Final settings recorded locally: {target}\n")
        self.log_msg(f"AutoTune final settings saved: {target}")

    def autotune_generate_report_thread(self):
        self.run_worker(self.autotune_generate_report)

    def autotune_generate_report(self):
        try:
            if self.autotune_baseline is None or self.autotune_load_step is None:
                raise RuntimeError("Capture baseline and load-step data before generating a report.")
            if self.autotune_analysis is None:
                self.autotune_analysis = analyze_autotune_session(self.autotune_baseline, self.autotune_load_step)
                self.ui_call(self.autotune_update_metric_tiles, self.autotune_analysis)
                self.ui_call(self.autotune_update_conditions, self.autotune_analysis)
            export_dir = self.autotune_session_folder()
            notes = self.autotune_notes.get("1.0", "end").strip()
            settings = self.autotune_settings_payload()
            technician = self.generator_site_vars.get("technician").get().strip() if self.generator_site_vars.get("technician") else ""
            files = write_autotune_package(
                export_dir,
                self.autotune_baseline,
                self.autotune_load_step,
                self.autotune_analysis,
                settings,
                notes,
                technician=technician,
            )
            self.latest_report_dir = export_dir
            self.register_report_session(export_dir, announce=True)
            self.log_msg(f"AutoTune commissioning report generated: {files['html']}")
            self.autotune_append_notes(f"Generated AutoTune report package: {export_dir}\n")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("AutoTune Report Error", exc)

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
            self.ensure_session(reason="generator report")
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
            if self.safe_199c_mode_var.get():
                self.log_msg(f"{SAFE_MODE_LABEL}: Connect Test uses ID at 1200 baud only; no GR, no PC 9600, no GL")
                ser, client, ident = self.connect_1200_id_only(xonxoff=False)
            else:
                ser, client, ident = self.connect_and_upgrade_baud()
            self.instrument_id = ident
            self.update_instrument_profile(
                ident=ident,
                port=self.port_var.get().strip(),
                baud=client.baudrate,
                safe_mode=self.safe_199c_mode_var.get(),
                remote_used=not self.safe_199c_mode_var.get(),
            )
            self.log_msg(f"Using active instrument profile: {self.active_profile_log_text()}")
            if self.session_dir is None or "NOT_CONNECTED" in str(self.outdir).upper():
                self.new_session(announce=False)
                self.log_msg(f"Identified scope; active session renamed for {scope_model_from_ident(ident)}: {self.outdir}")
            self.log_msg(f"Instrument: {ident}")
            self.log_msg(f"Active transfer baud: {client.baudrate}")
            self.ui_call(self.update_settings_display)
            self.set_connection_state("CONNECTED")
            self.log_result_state("CONNECT_TEST_PASS")
            self.log_msg("Connection test passed")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Connection Error", exc)
        finally:
            if ser is not None and client is not None:
                if self.safe_199c_mode_var.get():
                    self.close_serial_session_only(ser, "Legacy Safe ID Mode Connect Test")
                else:
                    self.release_serial_session(ser, client)

    def capture_screen_thread(self):
        self.run_worker(self.capture_screen)

    def single_waveform_report_thread(self):
        self.run_worker(self.single_waveform_report)

    def analyze_this_screen_thread(self):
        self.run_worker(self.analyze_this_screen)

    def live_waveform_analysis_thread(self):
        self.run_worker(lambda: self.analyze_this_screen(require_live=True, allow_png_fallback=False))

    def capture_screen(self):
        ser = None
        client = None
        safe_capture = self.safe_199c_mode_var.get()
        self.capture_active = True
        try:
            self.log_msg("Starting screen capture...")
            self.set_progress(0, 100, "Capture 0%")

            if safe_capture:
                self.log_msg(f"{SAFE_MODE_LABEL}: direct QP capture at 1200 baud; no GR, no ID, no PC 9600")
                ser, client, ident = self.connect_1200_for_direct_capture()
            else:
                ser, client, ident = self.connect_and_upgrade_baud(xonxoff=True)
            self.track_serial_session(ser, client)
            self.instrument_id = ident
            if self.session_dir is None or "NOT_CONNECTED" in str(self.outdir).upper():
                self.new_session(announce=False)
                self.log_msg(f"Identified scope; active session renamed for {scope_model_from_ident(ident)}: {self.outdir}")
            self.log_msg(f"Instrument: {ident}")
            self.ui_call(self.update_settings_display)
            raw, ser, client, ident = self.capture_screen_bytes(ser, client, ident)
            self.instrument_id = ident
            self.capture_index += 1
            raw_file = self.outdir / f"capture_{self.capture_index:03d}.bin"
            png_file = self.outdir / f"capture_{self.capture_index:03d}.png"

            raw_file.write_bytes(raw)
            self.last_raw = raw_file
            self.log_msg(f"Raw screen saved: {raw_file}")

            debug = self.save_screen_debug(raw, self.outdir)
            Image.open(debug["decoded_path"]).save(png_file)
            self.last_png = png_file

            self.log_msg(f"PNG saved: {png_file}")
            self.verify_capture_files(png_file=png_file, raw_file=raw_file)
            self.log_screen_debug(debug)
            self.confirm_decoded_full(debug)
            self.register_screen_capture_report(png_file, debug)
            self.show_image(png_file)
            self.log_msg("Screen Capture PASS: full image saved")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Capture Error", exc)
        finally:
            if ser is not None and client is not None:
                if safe_capture:
                    self.close_serial_session_only(ser, "Legacy Safe ID Mode Capture")
                else:
                    self.release_serial_session(ser, client)
            self.capture_active = False

    def analyze_this_screen(self, require_live=False, allow_png_fallback=True):
        ser = None
        client = None
        export_dir = None
        serial_opened = False
        started = time.monotonic()
        try:
            self.apply_calibration_settings(save=False)
            if self.capture_active:
                raise RuntimeError("Analyze is blocked while Capture is still active.")
            if not self.last_png or not Path(self.last_png).exists():
                raise RuntimeError("Capture a screen first. Analyze This Screen uses the latest captured screen plus fresh QW waveform data.")
            if require_live:
                if self.safe_199c_mode_var.get():
                    raise RuntimeError("Live Waveform Analysis requires normal connected-meter mode; safe mode is image-only.")
                if not str(self.port_var.get()).strip():
                    raise RuntimeError("Live Waveform Analysis requires a selected ScopeMeter COM port.")

            if self.safe_199c_mode_var.get():
                self.log_msg(f"Analyze This Screen: preparing PNG fallback report in {SAFE_MODE_LABEL}")
            else:
                self.log_msg("Analyze This Screen: capturing QW Channel A/B numeric waveform data")
            self.set_progress(text="Screen analysis...", indeterminate=True)
            time.sleep(0.75)
            self.ensure_session(reason="image-only screen report")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            export_dir = self.outdir / "reports" / f"screen_analysis_{ts}"
            export_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(self.last_png), export_dir / Path(self.last_png).name)
            shutil.copyfile(Path(self.last_png), export_dir / "screen_capture.png")

            if self.safe_199c_mode_var.get():
                self.log_msg(f"{SAFE_MODE_LABEL}: Analyze This Screen uses PNG fallback only; no GR, no QW, no live serial")
                fallback_text = self.visual_screen_analysis_fallback(
                    export_dir,
                    RuntimeError("Legacy Safe ID Mode blocks live QW analyzer commands."),
                )
                self.ui_call(self.set_waveform_text, fallback_text)
                self.set_connection_state("WARNING")
                self.set_progress(100, 100, "PNG fallback report")
                return

            ser, client, ident = self.get_or_connect_serial(xonxoff=True)
            serial_opened = True
            self.track_serial_session(ser, client)
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)

            from .waveform_protocol import analyze_voltage_current, compute_fft, query_waveform, thd_from_fft

            if time.monotonic() - started > 15.0:
                raise TimeoutError("Analyze timeout guard reached before QW download.")
            wf_a, raw_a = query_waveform(ser, client, "10")
            if time.monotonic() - started > 15.0:
                raise TimeoutError("Analyze timeout guard reached after Channel A.")
            wf_b, raw_b = query_waveform(ser, client, "20")
            (export_dir / "analyze_screen_channel_A_raw.bin").write_bytes(raw_a)
            (export_dir / "analyze_screen_channel_B_raw.bin").write_bytes(raw_b)
            self.write_screen_analysis_csv(export_dir / "analyze_screen_waveform.csv", wf_a, wf_b)

            analysis = self.compute_screen_power_quality(wf_a, wf_b, analyze_voltage_current, compute_fft, thd_from_fft)
            report_path = export_dir / "ANALYZE_THIS_SCREEN.txt"
            report_text = self.format_screen_analysis_report(ident, self.last_png, analysis)
            report_path.write_text(report_text, encoding="utf-8")

            self.ui_call(self.set_waveform_text, report_text)
            self.log_msg(f"Analyze This Screen report saved: {report_path}")
            self.log_msg(
                "Analyze This Screen PASS: "
                f"PF={self.format_number(analysis['power_factor'], 3)}, "
                f"phase={self.format_number(analysis['phase_i_minus_v_deg'], 2)} deg, "
                f"THD-V={self.format_number(analysis['thd_v_percent'], 2)} %, "
                f"THD-I={self.format_number(analysis['thd_i_percent'], 2)} %"
            )
            self.register_report_session(export_dir, announce=True)
            self.verify_capture_files(
                png_file=export_dir / "screen_capture.png",
                raw_file=export_dir / "analyze_screen_channel_A_raw.bin",
                txt_file=report_path,
            )
            self.set_progress(100, 100, "Screen analyzed")
        except Exception as exc:
            if not allow_png_fallback:
                self.log_msg(f"WARNING: Live waveform analysis failed; no fallback power-quality metrics were generated: {exc}")
                try:
                    if export_dir is None:
                        export_dir = self.outdir / "reports" / f"live_analysis_unavailable_{time.strftime('%Y-%m-%d_%H-%M-%S')}"
                        export_dir.mkdir(parents=True, exist_ok=True)
                    screen_target = Path(export_dir) / "screen_capture.png"
                    if self.last_png and Path(self.last_png).exists() and not screen_target.exists():
                        shutil.copyfile(Path(self.last_png), screen_target)
                    report_text = self.write_live_waveform_unavailable_report(export_dir, exc, serial_opened)
                    self.ui_call(self.set_waveform_text, report_text)
                    self.register_report_session(export_dir, announce=True)
                    self.set_connection_state("WARNING")
                    self.set_progress(100, 100, "Waveform unavailable")
                except Exception as report_exc:
                    self.log_msg(f"ERROR: {report_exc}")
                    self.show_error("Live Waveform Analysis Error", report_exc)
                return

            self.log_msg(f"WARNING: Numeric QW analysis failed; falling back to PNG visual analysis: {exc}")
            try:
                fallback_text = self.visual_screen_analysis_fallback(export_dir, exc)
                self.ui_call(self.set_waveform_text, fallback_text)
                self.set_connection_state("WARNING")
                self.set_progress(100, 100, "Image-only fallback report completed")
            except Exception as fallback_exc:
                self.log_msg(f"ERROR: {fallback_exc}")
                self.show_error("Analyze This Screen Error", fallback_exc)
        finally:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)
            self.capture_active = False

    def verify_capture_files(self, png_file=None, raw_file=None, html_file=None, txt_file=None):
        checks = [
            ("PNG", png_file),
            ("BIN/raw", raw_file),
            ("HTML report", html_file),
            ("TXT summary", txt_file),
        ]
        for label, path in checks:
            if path is None:
                continue
            path = Path(path)
            if path.exists() and path.stat().st_size > 0:
                if label == "PNG" and not self.valid_image_file(path):
                    self.log_msg(f"WARNING: {label} exists but is not a readable image: {path}")
                    continue
                self.log_msg(f"{label} verified: {path}")
            else:
                self.log_msg(f"WARNING: Capture completed but {label} was not saved. Output file path: {path}")

    def valid_image_file(self, path):
        try:
            with Image.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False

    def latest_session_png(self):
        if self.last_png and Path(self.last_png).exists():
            last_png = Path(self.last_png)
            if self.valid_image_file(last_png):
                return last_png
            self.log_msg(f"WARNING: latest screen image is corrupt/unreadable and will be ignored: {last_png}")

        candidates = []
        for root in [self.outdir, Path(self.outdir) / "reports"]:
            if Path(root).exists():
                candidates.extend(Path(root).rglob("*.png"))
        screen_names = {
            "screen_capture.png",
            "decoded_full.png",
            "rendered_preview.png",
        }
        candidates = [
            p for p in candidates
            if p.is_file()
            and (
                p.name in screen_names
                or p.name.startswith("capture_")
                or "_screen_" in p.name
                or p.name.endswith("_screen.png")
            )
        ]
        valid_candidates = [p for p in candidates if self.valid_image_file(p)]
        corrupt_count = len(candidates) - len(valid_candidates)
        if corrupt_count:
            self.log_msg(f"WARNING: ignored {corrupt_count} corrupt/unreadable PNG candidate(s)")
        return max(valid_candidates, key=lambda p: p.stat().st_mtime) if valid_candidates else None

    def visual_screen_analysis_fallback(self, export_dir, error):
        if export_dir is None:
            export_dir = self.unique_child_dir(
                self.outdir / "reports",
                f"screen_visual_{time.strftime('%Y-%m-%d_%H-%M-%S')}",
            )
            export_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.latest_session_png()
        if image_path is None:
            raise RuntimeError("No PNG available for visual waveform fallback.") from error
        screen_target = Path(export_dir) / "screen_capture.png"
        if image_path.resolve() != screen_target.resolve():
            shutil.copyfile(image_path, screen_target)

        ocr = self.extract_png_fallback_values(screen_target)
        self.write_png_fallback_summary_metrics(export_dir, ocr)
        ocr_lines = []
        for label, key in (
            ("Channel A volts", "channel_a_volts"),
            ("Channel B amps", "channel_b_amps"),
            ("Timebase", "timebase"),
            ("Frequency", "frequency_hz"),
        ):
            value = ocr.get(key)
            ocr_lines.append(f"{label}: {value if value else 'Unavailable from screenshot'}")

        report_text = "\n".join([
            "Analyze This Screen - Image Only Fallback Report",
            "================================================",
            "",
            "NO LIVE SCOPEMETER DATA USED. No GR, no QW, no serial capture, no real waveform samples. Metrics are limited to screenshot/image-mode status only.",
            "",
            "Source: PNG fallback only",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Scope ID: {self.instrument_id}",
            f"Screen image: {screen_target.name}",
            "Measurement source: latest saved PNG image only.",
            "Confidence: IMAGE-ONLY STATUS, NOT POWER-QUALITY ANALYSIS",
            f"Numeric QW failure: {error}",
            "",
            "Visible screen values:",
            *ocr_lines,
            "",
            "Voltage waveform: unavailable - no waveform samples",
            "Current waveform: unavailable - no waveform samples",
            "Harmonic content: unavailable - no FFT or QW data",
            "Power factor: unavailable - no voltage/current sample pair",
            "Generator stress: unavailable - image-only fallback cannot calculate load quality",
            "",
            "Generator Mode checklist:",
            "- Frequency stability",
            "- Voltage stability",
            "- Current distortion",
            "- Load step response",
            "- AVR hunting signs",
            "- Governor instability signs",
            "- Neutral current risk",
            "- Nonlinear load warning",
        ])
        report_path = Path(export_dir) / "ANALYZE_THIS_SCREEN.txt"
        report_path.write_text(report_text, encoding="utf-8")
        self.register_report_session(export_dir, announce=True)
        self.verify_capture_files(png_file=screen_target, txt_file=report_path)
        self.log_msg(f"PNG fallback analysis report saved: {report_path}")
        self.log_msg("Image-only fallback report completed.")
        return report_text

    def extract_png_fallback_values(self, image_path):
        result = {
            "ocr_available": False,
            "ocr_text": "",
            "channel_a_volts": "",
            "channel_b_amps": "",
            "timebase": "",
            "frequency_hz": "",
        }
        try:
            import pytesseract
        except Exception as exc:
            result["ocr_text"] = f"OCR unavailable: {exc}"
            self.log_msg("PNG fallback OCR unavailable; using explicit image-mode statuses.")
            return result

        try:
            image = Image.open(image_path).convert("L")
            scale = 3
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
            text = pytesseract.image_to_string(image)
            result["ocr_available"] = True
            result["ocr_text"] = text
            Path(image_path).with_name("png_fallback_ocr.txt").write_text(text, encoding="utf-8")
            self.log_msg("PNG fallback OCR attempted and saved to png_fallback_ocr.txt")
        except Exception as exc:
            result["ocr_text"] = f"OCR failed: {exc}"
            self.log_msg(f"PNG fallback OCR failed; using explicit image-mode statuses: {exc}")
            return result

        text = result["ocr_text"]
        compact = " ".join(text.replace("\n", " ").split())

        def first_match(patterns):
            for pattern in patterns:
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match:
                    return match.group(1).strip()
            return ""

        result["frequency_hz"] = first_match([
            r"([0-9]+(?:\.[0-9]+)?)\s*Hz",
        ])
        result["channel_a_volts"] = first_match([
            r"(?:A|CH\s*A|Channel\s*A)[^\d+-]*([+-]?[0-9]+(?:\.[0-9]+)?\s*(?:m?V|k?V))",
            r"([+-]?[0-9]+(?:\.[0-9]+)?\s*(?:m?V|k?V))",
        ])
        result["channel_b_amps"] = first_match([
            r"(?:B|CH\s*B|Channel\s*B)[^\d+-]*([+-]?[0-9]+(?:\.[0-9]+)?\s*(?:m?A|k?A|A))",
            r"([+-]?[0-9]+(?:\.[0-9]+)?\s*(?:m?A|k?A|A))",
        ])
        result["timebase"] = first_match([
            r"([0-9]+(?:\.[0-9]+)?\s*(?:n?s|u?s|m?s|s)\s*/\s*div)",
            r"([0-9]+(?:\.[0-9]+)?\s*(?:n?s|u?s|m?s|s))",
        ])
        return result

    def write_png_fallback_waveform_samples(self, export_dir):
        path = Path(export_dir) / "waveform_samples.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["placeholder_only", "analysis_mode", "status"])
            writer.writerow(["true", "PNG_FALLBACK_ONLY", "No real samples. No GR, no QW, no live serial capture."])
        return path

    def write_png_fallback_summary_metrics(self, export_dir, ocr):
        path = Path(export_dir) / "SUMMARY_METRICS.csv"
        frequency = f"{ocr['frequency_hz']} Hz (OCR)" if ocr.get("frequency_hz") else "Unavailable from screenshot"
        vrms = f"{ocr['channel_a_volts']} (OCR)" if ocr.get("channel_a_volts") else "N/A (image mode)"
        irms = f"{ocr['channel_b_amps']} (OCR)" if ocr.get("channel_b_amps") else "N/A (image mode)"
        fields = [
            "analysis_mode", "connected", "live_serial_used", "waveform_samples_valid", "power_quality_valid",
            "timestamp", "report_type", "report_source", "confidence", "scope_id", "frame_name",
            "vrms_v", "irms_a", "frequency_hz", "power_factor", "thd_v_percent", "thd_i_percent",
            "waveform", "voltage_quality", "timebase", "sample_count", "notes",
        ]
        row = {
            "analysis_mode": "PNG_FALLBACK_ONLY",
            "connected": "false",
            "live_serial_used": "false",
            "waveform_samples_valid": "false",
            "power_quality_valid": "false",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "Analyze This Screen - Image Only Fallback Report",
            "report_source": "PNG_FALLBACK_ONLY",
            "confidence": "Image-only fallback; no live waveform data",
            "scope_id": self.instrument_id,
            "frame_name": "png_fallback",
            "vrms_v": vrms,
            "irms_a": irms,
            "frequency_hz": frequency,
            "power_factor": "Unavailable - PNG fallback only",
            "thd_v_percent": "Unavailable - PNG fallback only",
            "thd_i_percent": "Unavailable - PNG fallback only",
            "waveform": "Unavailable - no real waveform samples",
            "voltage_quality": "Unavailable - image-only fallback",
            "timebase": ocr.get("timebase") or "Unavailable from screenshot",
            "sample_count": "Not captured",
            "notes": "NO LIVE SCOPEMETER DATA USED. No GR, no QW, no serial capture, no real waveform samples. Metrics are limited to screenshot/image-mode status only.",
        }
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        self.log_msg(f"PNG fallback summary metrics saved: {path}")
        return path

    def write_live_waveform_unavailable_report(self, export_dir, error, serial_opened):
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        report_path = export_dir / "ANALYZE_THIS_SCREEN.txt"
        summary_path = export_dir / "SUMMARY_METRICS.csv"
        report_text = "\n".join([
            "Live Waveform Analysis - Waveform Data Unavailable",
            "====================================================",
            "",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Scope ID: {self.instrument_id}",
            "Analysis mode: LIVE_WAVEFORM_REQUIRED",
            f"Live serial opened: {'true' if serial_opened else 'false'}",
            "",
            "Waveform data unavailable. No power-quality numbers were calculated.",
            "QW waveform capture did not produce valid Channel A and Channel B samples.",
            f"Failure: {error}",
            "",
            "Required for power-quality analysis:",
            "- GR/normal live communication available",
            "- QW Channel A numeric waveform samples",
            "- QW Channel B numeric waveform samples",
            "- Valid voltage/current sample pairs",
        ])
        report_path.write_text(report_text, encoding="utf-8")
        fields = [
            "analysis_mode", "connected", "live_serial_used", "waveform_samples_valid", "power_quality_valid",
            "timestamp", "report_type", "report_source", "confidence", "scope_id", "frame_name",
            "vrms_v", "irms_a", "frequency_hz", "power_factor", "thd_v_percent", "thd_i_percent",
            "waveform", "voltage_quality", "timebase", "sample_count", "notes",
        ]
        row = {
            "analysis_mode": "LIVE_WAVEFORM_UNAVAILABLE",
            "connected": "true" if serial_opened else "false",
            "live_serial_used": "true" if serial_opened else "false",
            "waveform_samples_valid": "false",
            "power_quality_valid": "false",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "Live Waveform Analysis - Waveform Data Unavailable",
            "report_source": "QW unavailable",
            "confidence": "No power-quality metrics calculated",
            "scope_id": self.instrument_id,
            "frame_name": "live_qw_unavailable",
            "vrms_v": "Unavailable - QW failed",
            "irms_a": "Unavailable - QW failed",
            "frequency_hz": "Unavailable - QW failed",
            "power_factor": "Unavailable - QW failed",
            "thd_v_percent": "Unavailable - QW failed",
            "thd_i_percent": "Unavailable - QW failed",
            "waveform": "Unavailable - QW failed",
            "voltage_quality": "Unavailable - QW failed",
            "timebase": "Unavailable - QW failed",
            "sample_count": "0",
            "notes": f"QW failed or live connection unavailable. No fallback power-quality values generated. Failure: {error}",
        }
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        self.log_msg(f"Live waveform unavailable report saved: {report_path}")
        return report_text

    def write_screen_analysis_csv(self, path, wf_a, wf_b):
        n = min(len(wf_a["x"]), len(wf_a["y"]), len(wf_b["y"]))
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "channel_a_voltage_v", "channel_b_current_a"])
            for idx in range(n):
                writer.writerow([float(wf_a["x"][idx]), float(wf_a["y"][idx]), float(wf_b["y"][idx])])

    def compute_screen_power_quality(self, wf_a, wf_b, analyze_voltage_current, compute_fft, thd_from_fft):
        import numpy as np

        n = min(len(wf_a["y"]), len(wf_b["y"]))
        if n < 16:
            raise RuntimeError("Not enough QW samples to analyze this screen.")
        x = np.asarray(wf_a["x"][:n], dtype=float)
        v = np.asarray(wf_a["y"][:n], dtype=float)
        i_raw = np.asarray(wf_b["y"][:n], dtype=float)
        i = i_raw * CURRENT_SCALE_A_PER_V
        finite = np.isfinite(x) & np.isfinite(v) & np.isfinite(i_raw)
        x = x[finite]
        v = v[finite]
        i_raw = i_raw[finite]
        i = i[finite]
        if len(v) < 16:
            raise RuntimeError("Not enough finite QW samples to analyze this screen.")

        power = analyze_voltage_current(v, i_raw, wf_a["delta_x"])
        freq_v, amp_v, _fs_v = compute_fft(x, v)
        freq_i, amp_i, _fs_i = compute_fft(x, i)
        thd_v, _f1_v, a1_v, harmonics_v = thd_from_fft(freq_v, amp_v)
        thd_i, _f1_i, a1_i, harmonics_i = thd_from_fft(freq_i, amp_i)
        vrms = float(power["vrms_v"])
        irms = float(power["irms_a"])
        crest_v = float(np.nanmax(np.abs(v)) / vrms) if vrms > 0 else float("nan")
        crest_i = float(np.nanmax(np.abs(i)) / irms) if irms > 0 else float("nan")

        odd_suspicion = []
        for harmonic in (3, 5, 7, 9, 11, 13):
            amp = harmonics_i.get(harmonic, 0.0)
            pct = (amp / a1_i * 100.0) if a1_i > 0 else float("nan")
            if np.isfinite(pct) and pct >= 8.0:
                odd_suspicion.append((harmonic, pct))

        thd_i_percent = float(thd_i * 100.0) if np.isfinite(thd_i) else float("nan")
        thd_v_percent = float(thd_v * 100.0) if np.isfinite(thd_v) else float("nan")
        nonlinear = (
            (np.isfinite(thd_i_percent) and thd_i_percent >= 15.0)
            or (np.isfinite(crest_i) and crest_i >= 1.8)
            or bool(odd_suspicion)
        )
        harmonic_suspicion = (
            "YES" if (np.isfinite(thd_i_percent) and thd_i_percent >= 12.0) or odd_suspicion else "NO"
        )

        return {
            **power,
            "thd_v_percent": thd_v_percent,
            "thd_i_percent": thd_i_percent,
            "crest_factor_v": crest_v,
            "crest_factor_i": crest_i,
            "harmonic_suspicion": harmonic_suspicion,
            "odd_harmonic_suspects": odd_suspicion,
            "nonlinear_load_signature": "YES" if nonlinear else "NO",
        }

    def format_number(self, value, digits=2):
        try:
            if math.isfinite(float(value)):
                return f"{float(value):.{digits}f}"
        except Exception:
            pass
        return "n/a"

    def format_screen_analysis_report(self, ident, screen_path, analysis):
        suspects = analysis["odd_harmonic_suspects"]
        suspect_text = ", ".join(f"H{h}={pct:.1f}%" for h, pct in suspects) if suspects else "None above threshold"
        thd_i = analysis.get("thd_i_percent", float("nan"))
        thd_v = analysis.get("thd_v_percent", float("nan"))
        crest_i = analysis.get("crest_factor_i", float("nan"))
        pf = analysis.get("power_factor", float("nan"))
        harmonic_content = "unknown"
        if math.isfinite(float(thd_i)):
            harmonic_content = "high" if thd_i >= 20 else "moderate" if thd_i >= 8 else "low"
        voltage_waveform = "distorted" if math.isfinite(float(thd_v)) and thd_v >= 8 else "normal"
        current_waveform = "nonlinear" if analysis["nonlinear_load_signature"] == "YES" else "normal"
        if math.isfinite(float(crest_i)) and crest_i >= 2.5:
            current_waveform = "pulsed"
        pf_estimate = "unknown"
        if math.isfinite(float(pf)):
            pf_estimate = "lagging" if analysis.get("phase_i_minus_v_deg", 0) < 0 else "leading"
        generator_stress = "high" if harmonic_content == "high" or current_waveform in ("pulsed", "clipped") else "medium" if harmonic_content == "moderate" else "low"
        return "\n".join([
            "ANALYZE THIS SCREEN",
            "===================",
            "",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Scope ID: {ident}",
            f"Screen image: {Path(screen_path).name}",
            "Measurement source: QW numeric waveform data, not screen pixels.",
            "",
            f"Voltage RMS: {self.format_number(analysis['vrms_v'], 3)} V",
            f"Current RMS: {self.format_number(analysis['irms_a'], 3)} A",
            f"Real Power: {self.format_number(analysis['real_power_w'], 3)} W",
            f"Apparent Power: {self.format_number(analysis['apparent_power_va'], 3)} VA",
            f"Reactive Power: {self.format_number(analysis['reactive_power_var'], 3)} VAR",
            f"Voltage/Current Phase Angle I-V: {self.format_number(analysis['phase_i_minus_v_deg'], 2)} deg",
            f"Power Factor: {self.format_number(analysis['power_factor'], 4)}",
            f"Fundamental Frequency: {self.format_number(analysis['fundamental_hz'], 3)} Hz",
            f"THD-V: {self.format_number(analysis['thd_v_percent'], 2)} %",
            f"THD-I: {self.format_number(analysis['thd_i_percent'], 2)} %",
            f"Voltage Crest Factor: {self.format_number(analysis['crest_factor_v'], 3)}",
            f"Current Crest Factor: {self.format_number(analysis['crest_factor_i'], 3)}",
            f"Harmonic Suspicion: {analysis['harmonic_suspicion']}",
            f"Odd Harmonic Suspects: {suspect_text}",
            f"Nonlinear Load Signature: {analysis['nonlinear_load_signature']}",
            "",
            f"Voltage waveform: {voltage_waveform}",
            f"Current waveform: {current_waveform}",
            f"Likely harmonic content: {harmonic_content}",
            f"Power factor estimate: {pf_estimate}",
            f"Generator stress: {generator_stress}",
            "",
            "Generator Mode checklist:",
            "- Frequency stability",
            "- Voltage stability",
            "- Current distortion",
            "- Load step response",
            "- AVR hunting signs",
            "- Governor instability signs",
            "- Neutral current risk",
            "- Nonlinear load warning",
            "",
            "Notes:",
            "- Harmonic suspicion is rule-based and intended for field screening.",
            "- Confirm final harmonic compliance with a dedicated power-quality instrument if required.",
        ])

    def set_waveform_text(self, text):
        self.waveform_text.configure(state="normal")
        self.waveform_text.delete("1.0", "end")
        self.waveform_text.insert("end", text)
        self.waveform_text.configure(state="disabled")

    def log_result_state(self, state):
        self.log_msg(f"Result State: {state}")
        self.status_var.set(state)

    def single_waveform_report(self):
        ser = None
        client = None
        export_dir = None
        safe_single = self.safe_199c_mode_var.get() and not self.advanced_transfer_mode_var.get()
        try:
            self.apply_calibration_settings(save=False)
            self.log_msg("Starting single screen waveform report...")
            self.set_progress(0, 100, "Single report 0%")
            self.ensure_session(reason="single waveform report")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            export_dir = self.outdir / "reports" / f"single_{ts}"
            export_dir.mkdir(parents=True, exist_ok=True)
            self.log_msg(f"Report output folder: {export_dir}")

            if safe_single:
                self.log_msg(
                    f"{SAFE_MODE_LABEL}: Single Waveform Report uses ID/QW only; "
                    f"no GR, no PC {WORK_BAUD}. Default baud={INITIAL_BAUD}; known PC-ACK baud may be reused."
                )
                ser, client, ident = self.connect_safe_id_known_baud_recovery(xonxoff=False)
                self.track_serial_session(ser, client)
                self.instrument_id = ident
                model = scope_model_from_ident(ident)
                self.update_instrument_profile(
                    ident=ident,
                    port=self.port_var.get().strip(),
                    baud=client.baudrate,
                    safe_mode=True,
                    remote_used=False,
                )
                self.log_msg(f"Using active instrument profile: {self.active_profile_log_text()}")
                self.log_msg(f"{SAFE_MODE_LABEL}: confirmed model FLUKE {model} at {client.baudrate} baud")
                self.ui_call(self.update_settings_display)
                self.set_progress(15, 100, "Safe mode ID confirmed")

                wf_a = None
                wf_b = None
                raw_a = None
                raw_b = None
                q_w_errors = []
                from .waveform_protocol import query_waveform, save_single_waveform_report

                try:
                    self.log_msg(f"{SAFE_MODE_LABEL}: trying QW 10 at {client.baudrate} baud")
                    wf_a, raw_a = query_waveform(ser, client, "10")
                    (export_dir / "single_waveform_A_raw.bin").write_bytes(raw_a)
                    self.set_progress(55, 100, "Channel A captured")
                except Exception as exc:
                    q_w_errors.append(f"Channel A QW failed: {exc}")
                    self.log_msg(q_w_errors[-1])

                try:
                    self.log_msg(f"{SAFE_MODE_LABEL}: trying QW 20 at {client.baudrate} baud")
                    wf_b, raw_b = query_waveform(ser, client, "20")
                    (export_dir / "single_waveform_B_raw.bin").write_bytes(raw_b)
                    self.set_progress(80, 100, "Channel B captured")
                except Exception as exc:
                    q_w_errors.append(f"Channel B QW failed: {exc}")
                    self.log_msg(q_w_errors[-1])

                if wf_a is None and wf_b is None:
                    report_path = self.write_safe_mode_waveform_unavailable_report(
                        export_dir,
                        ident,
                        q_w_errors,
                        client.baudrate,
                    )
                    self.ui_call(self.set_waveform_text, report_path.read_text(encoding="utf-8"))
                    self.register_report_session(export_dir, announce=True)
                    self.log_result_state("WAVEFORM_REPORT_UNAVAILABLE")
                    self.set_progress(100, 100, "Waveform unavailable")
                    return

                report_path = save_single_waveform_report(
                    export_dir,
                    ident,
                    None,
                    wf_a=wf_a,
                    wf_b=wf_b,
                    visual_only=False,
                    q_w_error="; ".join(q_w_errors) if q_w_errors else None,
                    log=self.log_msg,
                )
                self.log_msg(f"Single waveform report saved: {report_path}")
                self.build_professional_report(export_dir, ident, "Single Screen Waveform Report")
                self.register_report_session(export_dir, announce=True)
                self.log_result_state("WAVEFORM_REPORT_PASS")
                self.set_progress(100, 100, "Single report complete")
                return

            ser, client, ident = self.connect_and_upgrade_baud(xonxoff=True)
            self.track_serial_session(ser, client)
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)

            raw, ser, client, ident = self.capture_screen_bytes(ser, client, ident)
            self.instrument_id = ident
            debug = self.save_screen_debug(raw, export_dir)
            prefix = screen_capture_filename_prefix(ident)
            screen_capture = export_dir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            Image.open(debug["decoded_path"]).save(screen_capture)
            shutil.copyfile(screen_capture, export_dir / "screen_capture.png")
            self.last_png = screen_capture
            self.last_raw = debug["raw_path"]
            self.log_screen_debug(debug)
            self.confirm_decoded_full(debug)
            self.show_image(screen_capture)
            self.log_msg("Screen Capture PASS: full image saved")
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
            self.log_result_state("WAVEFORM_REPORT_PASS")
            self.set_progress(100, 100, "Single report complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.log_result_state("WAVEFORM_REPORT_FAIL")
            if export_dir and any(Path(export_dir).iterdir()):
                try:
                    self.register_report_session(export_dir, announce=True)
                except Exception:
                    pass
            self.show_error("Single Waveform Report Error", exc)
        finally:
            if ser is not None and client is not None:
                if safe_single:
                    self.close_serial_session_only(ser, "Legacy Safe ID Mode Single Waveform Report")
                else:
                    self.release_serial_session(ser, client)

    def write_safe_mode_waveform_unavailable_report(self, export_dir, ident, q_w_errors, baud):
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        report_path = export_dir / "WAVEFORM_REPORT_UNAVAILABLE.txt"
        message = f"Waveform data unavailable in {SAFE_MODE_LABEL}. Use Replay Capture or Image-Only Screen Snapshot."
        lines = [
            "Single Screen Waveform Report - Waveform Data Unavailable",
            "==========================================================",
            "",
            f"Result State: WAVEFORM_REPORT_UNAVAILABLE",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Scope ID: {ident}",
            f"Model: FLUKE {scope_model_from_ident(ident)}",
            f"Port: {self.port_var.get().strip() or 'None selected'}",
            f"Baud attempted: {baud}",
            "",
            message,
            "",
            "Legacy safe-mode transfer rules used:",
            "- ID at 1200 baud only",
            "- QW 10 and QW 20 attempted at the confirmed working baud",
            "- GR was not issued",
            f"- PC {WORK_BAUD} was not issued",
            "- No screenshot or professional waveform report was generated because waveform acquisition never started successfully",
            "",
            "QW result:",
            *([f"- {err}" for err in q_w_errors] if q_w_errors else ["- QW unavailable"]),
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        self.log_msg(message)
        self.log_msg(f"Safe-mode diagnostic report saved: {report_path}")
        return report_path

    def scope_model_from_ident(self, ident):
        return scope_model_from_ident(ident)

    def screen_capture_mode_for_ident(self, ident):
        return screen_capture_mode_for_ident(ident)

    def capture_screen_once(self, ser, client, ident):
        mode, png_supported, model = self.screen_capture_mode_for_ident(ident)
        self.log_msg(f"Detected model: {model}")
        self.log_msg(f"Active transfer baud: {client.baudrate}")

        methods = [
            ("QP 0,11,B", "png"),
            ("QP 0", "raster"),
            ("QP", "raster"),
            ("HC", "raster"),
        ]
        failures = []
        for command, payload_kind in methods:
            try:
                self.log_msg(f"Capture command sent: {command}")
                if payload_kind == "png" and getattr(ser, "xonxoff", False):
                    self.log_msg("PNG block transfer: disabling serial XON/XOFF so binary PNG bytes are preserved")
                    ser.xonxoff = False
                client.send_cmd(ser, command)
                self.log_msg("Capture command accepted")
                if payload_kind == "png":
                    self.log_msg("PNG framing stage 1: command ACK accepted")
                self.log_msg("Binary transfer started")
                if payload_kind == "png":
                    raw = client.read_qp_png_payload(ser, self.progress_callback("Capture"))
                    self.log_msg("PNG decoded")
                else:
                    raw = client.read_qp_payload(ser, self.progress_callback("Capture"))
                self.log_msg("Binary transfer complete")
                return raw
            except Exception as exc:
                failures.append(f"{command}: {exc}")
                self.log_msg(f"WARNING: capture method failed: {command}: {exc}")
                if payload_kind == "png":
                    if "ACK=b'1'" in str(exc) or 'ACK=b"1"' in str(exc):
                        raise RuntimeError(
                            "QP 0,11,B returned ACK=1 after a previous failed binary transfer. "
                            "This indicates the ScopeMeter may still be in a dirty transfer state. "
                            "Power-cycle the ScopeMeter, return to the live waveform screen, close menus, "
                            "exit replay/recall, make sure HOLD is off, then try capture again."
                        ) from exc
                    raise RuntimeError(
                        "PNG block transfer failed; serial session is dirty. "
                        "The port must be aborted, drained, and closed before another QP attempt."
                    ) from exc
                if isinstance(exc, TimeoutError):
                    raise
        raise RuntimeError("All screen capture methods failed: " + " | ".join(failures))

    def recover_screen_capture_session(self, ser, client):
        self.log_msg("Screen capture recovery: treating QP failure as meter/session state, not incompatibility")
        self.log_msg("Screen capture recovery: sending GL if possible, closing port, waiting 1.5 seconds")
        if ser is not None and client is not None:
            self.release_serial_session(ser, client)
        time.sleep(1.5)
        new_ser, new_client, new_ident = self.connect_and_upgrade_baud(xonxoff=True)
        self.track_serial_session(new_ser, new_client)
        self.instrument_id = new_ident
        self.log_msg(f"Meter identified: {new_ident}")
        self.log_msg(f"Screen capture recovery: reopened at last known baud path; active baud {new_client.baudrate}")
        self.ui_call(self.update_settings_display)
        return new_ser, new_client, new_ident

    def capture_screen_bytes(self, ser, client, ident):
        retry_count = 0
        try:
            self.log_msg(f"Screen capture retry count: {retry_count}")
            raw = self.capture_screen_once(ser, client, ident)
            return raw, ser, client, ident
        except Exception as exc:
            self.log_msg(f"Screen capture transfer failed: {exc}")
            self.set_connection_state("METER LOCKED / RECOVERY NEEDED")
            self.cleanup_dirty_binary_session(ser)
            raise RuntimeError(
                "Screen capture failed during PNG binary transfer. The app aborted the transfer, "
                "drained pending bytes, and closed the port. No QP retry was attempted in the same dirty session. "
                "If the next QP attempt returns ACK=1, power-cycle the ScopeMeter, return to the live waveform "
                "screen, close menus, exit replay/recall, make sure HOLD is off, then try again."
            ) from exc

    def get_or_connect_serial(self, xonxoff=False):
        if self.active_serial is not None and getattr(self.active_serial, "is_open", False) and self.active_client is not None:
            self.log_msg("Reusing existing serial session")
            return self.active_serial, self.active_client, self.instrument_id
        self.log_msg("No open serial session available; connecting")
        return self.connect_and_upgrade_baud(xonxoff=xonxoff)

    def connect_id_only_at_baud(self, baud=INITIAL_BAUD, xonxoff=False, label=None, update_safe_profile=True):
        port = self.port_var.get().strip()
        if not port:
            raise RuntimeError("No serial port selected.")
        if self.active_serial is not None:
            self.log_msg(f"{SAFE_MODE_LABEL}: closing old serial handle before ID-only connect")
            self.close_serial_session_only(self.active_serial, "Legacy Safe ID Mode old session")

        baud = int(baud or INITIAL_BAUD)
        label = label or f"{SAFE_MODE_LABEL}: opening serial port {port} at {baud} baud for ID only"
        self.log_msg(label)
        client = self.serial_client(baudrate=baud, xonxoff=xonxoff)
        ser = client.open()
        self.log_msg(f"{SAFE_MODE_LABEL}: port opened: {port} at {baud}")
        self.track_serial_session(ser, client)
        try:
            time.sleep(0.3)
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            ack, ident = self.safe_id_query_at_current_baud(ser, client)
            self.log_msg(f"{SAFE_MODE_LABEL}: ID ACK={ack!r}")
            ident_clean = self.clean_ident_response(ident)
            if not ident_clean.upper().startswith("FLUKE"):
                raise RuntimeError(f"Unexpected ID response: {ident!r}")
            self.current_scope_baud = baud
            self.instrument_id = ident_clean
            profile = self.update_instrument_profile(
                ident=ident_clean,
                port=port,
                baud=baud,
                safe_mode=bool(update_safe_profile),
                remote_used=False,
            )
            model = profile["model"]
            self.log_msg(f"{SAFE_MODE_LABEL}: meter identified: FLUKE {model}")
            self.log_msg(f"Using active instrument profile: {self.active_profile_log_text(profile)}")
            return ser, client, ident_clean
        except Exception:
            self.close_serial_session_only(ser, "Legacy Safe ID Mode Connect Test failed")
            raise

    def connect_1200_id_only(self, xonxoff=False):
        return self.connect_id_only_at_baud(
            INITIAL_BAUD,
            xonxoff=xonxoff,
            label=f"{SAFE_MODE_LABEL}: opening serial port {self.port_var.get().strip()} at {INITIAL_BAUD} baud for ID only",
            update_safe_profile=True,
        )

    def connect_safe_id_known_baud_recovery(self, xonxoff=False):
        candidates = []

        profile = self.instrument_profile or {}
        profile_baud = int(profile.get("baud") or 0)
        if profile.get("safe_mode") and profile.get("ident") and profile_baud:
            candidates.append((profile_baud, "saved safe profile"))

        candidates.append((INITIAL_BAUD, "default safe baud"))

        for baud, reason in (
            (profile_baud, "last saved profile baud"),
            (int(self.current_scope_baud or 0), "last known PC-ACK baud"),
        ):
            if baud and baud != INITIAL_BAUD and all(existing != baud for existing, _ in candidates):
                candidates.append((baud, reason))

        last_error = None
        for baud, reason in candidates:
            try:
                if baud != INITIAL_BAUD:
                    self.log_msg(
                        f"{SAFE_MODE_LABEL}: 1200 baud may be unavailable after prior PC ACK; "
                        f"trying {reason} {baud} baud with ID only, no GR, no PC"
                    )
                return self.connect_id_only_at_baud(
                    baud,
                    xonxoff=xonxoff,
                    label=f"{SAFE_MODE_LABEL}: opening serial port {self.port_var.get().strip()} at {baud} baud for ID only ({reason})",
                    update_safe_profile=True,
                )
            except Exception as exc:
                last_error = exc
                self.log_msg(f"{SAFE_MODE_LABEL}: ID-only connect failed at {baud} baud ({reason}): {exc}")
        raise last_error or RuntimeError("Legacy Safe ID Mode could not identify the ScopeMeter at known bauds.")

    def clean_ident_response(self, ident):
        text = str(ident or "").replace("\x00", "").strip()
        match = re.search(r"FLUKE\s+[A-Z0-9]+(?:;[^\r\n\x00]*)?", text, flags=re.IGNORECASE)
        if not match:
            return text
        return match.group(0).strip()

    def reconnect_using_active_profile(self, xonxoff=False):
        profile = self.instrument_profile
        port = profile.get("port") or self.port_var.get().strip()
        baud = int(profile.get("baud") or INITIAL_BAUD)
        if not port:
            raise RuntimeError("No saved instrument profile port is available.")
        if self.port_var.get().strip() != port:
            self.port_var.set(port)
        if self.active_serial is not None:
            label = f"{SAFE_MODE_LABEL} old session" if profile.get("safe_mode") else "Old serial session"
            if profile.get("safe_mode"):
                self.close_serial_session_only(self.active_serial, label)
            else:
                self.release_serial_session(self.active_serial, self.active_client)
        self.log_msg(f"Using active instrument profile: {self.active_profile_log_text(profile)}")
        self.log_msg(f"{SAFE_MODE_LABEL}: reconnecting exactly at {port} / {baud} baud; no baud scan")
        client = self.serial_client(baudrate=baud, xonxoff=xonxoff)
        ser = client.open()
        self.track_serial_session(ser, client)
        self.current_scope_baud = baud
        try:
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            ack, ident = self.safe_id_query_at_current_baud(ser, client)
            ident_clean = self.clean_ident_response(ident)
            if ack != b"0" or not ident_clean.upper().startswith("FLUKE"):
                raise RuntimeError(f"Unexpected ID response using saved profile: ACK={ack!r}, ID={ident!r}")
            self.instrument_id = ident_clean
            self.update_instrument_profile(
                ident=ident_clean,
                port=port,
                baud=baud,
                safe_mode=profile.get("safe_mode", True),
                remote_used=profile.get("remote_used", False),
            )
            self.log_msg(f"{SAFE_MODE_LABEL}: saved profile ID confirmed: {ident_clean}")
            return ser, client, ident_clean
        except Exception:
            self.close_serial_session_only(ser, f"{SAFE_MODE_LABEL} saved profile reconnect failed")
            raise

    def safe_id_query_at_current_baud(self, ser, client, attempts=2):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                try:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception:
                    pass
                if attempt > 1:
                    self.log_msg(f"{SAFE_MODE_LABEL}: retrying ID at {client.baudrate} baud after drain; no GR, no baud scan")
                    time.sleep(0.5)
                ack, ident = client.query_ascii_allow_ack(ser, "ID", timeout=8.0, clear_input=True)
                ident_clean = self.clean_ident_response(ident)
                if not ident_clean.upper().startswith("FLUKE"):
                    raise RuntimeError(f"Unexpected ID response: {ident!r}")
                return ack, ident_clean
            except Exception as exc:
                last_error = exc
                self.log_msg(f"{SAFE_MODE_LABEL}: ID attempt {attempt} failed at {client.baudrate} baud: {exc}")
                try:
                    old_timeout = getattr(ser, "timeout", None)
                    ser.timeout = 0.1
                    drained = 0
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        chunk = ser.read(256)
                        if not chunk:
                            break
                        drained += len(chunk)
                    self.log_msg(f"{SAFE_MODE_LABEL}: drained {drained} stale byte(s) before ID retry")
                    if old_timeout is not None:
                        ser.timeout = old_timeout
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception as drain_exc:
                    self.log_msg(f"{SAFE_MODE_LABEL}: stale-byte drain failed: {drain_exc}")
        raise last_error or RuntimeError("ID failed in Legacy Safe ID Mode.")

    def connect_1200_for_direct_capture(self):
        profile = self.instrument_profile if self.instrument_profile.get("safe_mode") else {}
        port = profile.get("port") or self.port_var.get().strip()
        if not port:
            raise RuntimeError("No serial port selected.")
        if self.port_var.get().strip() != port:
            self.port_var.set(port)
        if self.active_serial is not None:
            self.log_msg(f"{SAFE_MODE_LABEL}: closing old serial handle before direct capture")
            self.close_serial_session_only(self.active_serial, "Legacy Safe ID Mode old session")

        baud = int(profile.get("baud") or INITIAL_BAUD)
        self.log_msg(f"Using active instrument profile: {self.active_profile_log_text(profile or None)}")
        self.log_msg(f"{SAFE_MODE_LABEL}: opening serial port {port} at {baud} baud for direct QP")
        client = self.serial_client(baudrate=baud, xonxoff=False)
        ser = client.open()
        self.log_msg(f"{SAFE_MODE_LABEL}: port opened: {port} at {baud}")
        self.track_serial_session(ser, client)
        self.current_scope_baud = baud
        ident = self.instrument_id if str(self.instrument_id).upper().startswith("FLUKE") else "FLUKE 19X;SAFE_MODE"
        self.update_instrument_profile(ident=ident, port=port, baud=baud, safe_mode=True, remote_used=False)
        return ser, client, ident

    def connect_and_upgrade_baud(self, xonxoff=False):
        port = self.port_var.get().strip()
        if not port:
            raise RuntimeError("No serial port selected.")
        if self.active_serial is not None:
            self.log_msg("Closing old serial handle before reconnecting")
            self.release_serial_session(self.active_serial, self.active_client)

        def close_without_gl(open_ser):
            if open_ser is None:
                return
            try:
                open_ser.flush()
                open_ser.reset_input_buffer()
                open_ser.reset_output_buffer()
            except Exception:
                pass
            try:
                open_ser.close()
            except Exception:
                pass
            if open_ser is self.active_serial:
                self.active_serial = None
                self.active_client = None

        def try_identify_at_baud(baud):
            ser = None
            client = None
            self.log_msg(f"Trying baud {baud}")
            try:
                self.log_msg(f"Opening serial port {port} at {baud} baud")
                client = self.serial_client(baudrate=baud, xonxoff=xonxoff)
                ser = client.open()
                self.log_msg(f"Port opened: {port} at {baud}")
                self.track_serial_session(ser, client)
                time.sleep(0.5)
                try:
                    client.send_cmd(ser, "GR")
                    ident = client.query_ascii(ser, "ID")
                except Exception as gr_exc:
                    self.log_msg(f"GR rejected, trying ID anyway: {gr_exc}")
                    ack, ident = client.query_ascii_allow_ack(ser, "ID")
                    if ack == b"0" and ident.strip().upper().startswith("FLUKE"):
                        self.log_msg(f"ID succeeded; scope already reachable at {baud}")
                    else:
                        raise
                self.log_msg(f"Meter identified: {ident}")
                self.log_msg(f"Scope responded at baud {baud}")
                return ser, client, ident
            except Exception as exc:
                self.log_msg(f"No response at baud {baud}: {exc}")
                close_without_gl(ser)
                return None, None, None

        self.log_msg(f"Last known baud: {self.current_scope_baud}")
        candidates = []
        if self.current_scope_baud:
            candidates.append(self.current_scope_baud)
        for baud in (WORK_BAUD, INITIAL_BAUD):
            if baud not in candidates:
                candidates.append(baud)

        ser = None
        client = None
        ident = None
        connected_baud = None
        try:
            for baud in candidates:
                ser, client, ident = try_identify_at_baud(baud)
                if ser is not None:
                    connected_baud = baud
                    break

            if ser is None or client is None:
                self.current_scope_baud = INITIAL_BAUD
                raise RuntimeError("Unable to identify ScopeMeter at last known, work, or initial baud.")

            self.current_scope_baud = connected_baud
            if connected_baud == WORK_BAUD or WORK_BAUD == INITIAL_BAUD:
                self.log_msg(f"Instrument confirmed at {connected_baud}: {ident}")
                self.log_msg(f"Active transfer baud: {connected_baud}")
                self.update_instrument_profile(
                    ident=ident,
                    port=port,
                    baud=connected_baud,
                    safe_mode=False,
                    remote_used=True,
                )
                return ser, client, ident

            try:
                self.log_msg(f"Instrument identified at {connected_baud}: {ident}")
                self.log_msg(f"Requesting baud upgrade: PC {WORK_BAUD}")
                client.send_cmd(ser, f"PC {WORK_BAUD}")
            except Exception as exc:
                self.log_msg(f"WARNING: PC {WORK_BAUD} failed; falling back to {connected_baud}: {exc}")
                self.current_scope_baud = connected_baud
                self.log_msg(f"Active transfer baud: {connected_baud}")
                self.update_instrument_profile(
                    ident=ident,
                    port=port,
                    baud=connected_baud,
                    safe_mode=False,
                    remote_used=True,
                )
                return ser, client, ident

            self.current_scope_baud = WORK_BAUD
            self.log_msg(f"Closing {connected_baud} baud session")
            close_without_gl(ser)
            ser = None
            client = None
            time.sleep(0.8)

            self.log_msg(f"Reopening serial port {port} at {WORK_BAUD} baud")
            client = self.serial_client(baudrate=WORK_BAUD, xonxoff=xonxoff)
            ser = client.open()
            self.log_msg(f"Port opened: {port} at {WORK_BAUD}")
            self.track_serial_session(ser, client)
            time.sleep(0.5)
            try:
                client.send_cmd(ser, "GR")
                ident = client.query_ascii(ser, "ID")
            except Exception as gr_exc:
                self.log_msg(f"GR rejected, trying ID anyway: {gr_exc}")
                ack, ident = client.query_ascii_allow_ack(ser, "ID")
                if ack == b"0" and ident.strip().upper().startswith("FLUKE"):
                    self.log_msg(f"ID succeeded; scope already reachable at {WORK_BAUD}")
                else:
                    raise
            self.log_msg(f"Instrument confirmed at {WORK_BAUD}: {ident}")
            self.log_msg(f"Scope responded at baud {WORK_BAUD}")
            self.log_msg(f"Active transfer baud: {WORK_BAUD}")
            self.update_instrument_profile(
                ident=ident,
                port=port,
                baud=WORK_BAUD,
                safe_mode=False,
                remote_used=True,
            )
            return ser, client, ident
        except Exception:
            if ser is not None and client is not None:
                self.release_serial_session(ser, client)
            raise

    def connect_screen_serial(self):
        return self.connect_and_upgrade_baud(xonxoff=True)

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
            title="Load saved capture",
            filetypes=(
                ("Saved captures", "*.bin *.raw *.dat *.png *.jpg *.jpeg *.bmp *.csv"),
                ("Screen captures", "*.bin *.png *.jpg *.jpeg *.bmp"),
                ("Waveform files", "*.bin *.raw *.dat *.csv"),
                ("All files", "*.*"),
            ),
        )
        if not path:
            return
        self.run_worker(lambda: self.load_saved_capture(Path(path), debug_mode=False))

    def replay_debug_capture_thread(self):
        if not self.enable_capture_debug_mode_var.get():
            messagebox.showinfo("Debug Mode Disabled", "Enable Debug Mode in Settings to use this loader.")
            return
        path = filedialog.askopenfilename(
            initialdir=str(self.outdir),
            title="Load capture in debug mode",
            filetypes=(
                ("Capture/debug files", "*.bin *.raw *.dat *.png *.jpg *.jpeg *.bmp *.csv"),
                ("All files", "*.*"),
            ),
        )
        if not path:
            return
        self.run_worker(lambda: self.load_saved_capture(Path(path), debug_mode=True))

    def replay_capture(self, raw_path):
        self.load_saved_capture(raw_path, debug_mode=True)

    def load_saved_capture(self, path, debug_mode=False):
        path = Path(path)
        image_ext = {".png", ".jpg", ".jpeg", ".bmp"}
        try:
            self.log_msg(f"Loading saved capture: {path}")
            self.set_progress(text="Loading saved capture...", indeterminate=True)
            if not path.exists():
                raise FileNotFoundError(f"Capture file not found: {path}")
            if path.stat().st_size <= 0:
                raise RuntimeError(f"Capture file is empty: {path}")

            if path.suffix.lower() in image_ext:
                self.load_saved_screen_image(path, debug_mode=debug_mode)
                return

            try:
                self.load_saved_screen_bytes(path, debug_mode=debug_mode)
                return
            except Exception as screen_exc:
                if debug_mode:
                    self.log_msg(f"Debug mode: screen decode failed, trying waveform parser: {screen_exc}")
                self.log_msg("Saved capture did not decode as a screen image; treating it as raw waveform data.")
                self.load_saved_waveform_capture(path)
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Load Saved Capture Error", exc)

    def load_saved_screen_image(self, image_path, debug_mode=False):
        self.set_progress(text="Loading saved screen...", indeterminate=True)
        with Image.open(image_path) as img:
            img.verify()
        self.last_png = Path(image_path)
        self.last_raw = Path(image_path)
        self.log_msg("Loaded capture successfully")
        self.show_image(image_path)
        self.finish_loaded_screen_preview(image_path)

    def load_saved_screen_bytes(self, raw_path, debug_mode=False):
        try:
            self.set_progress(text="Loading saved screen...", indeterminate=True)
            if not raw_path.exists():
                raise FileNotFoundError(f"Capture file not found: {raw_path}")
            raw = raw_path.read_bytes()
            if not raw:
                raise RuntimeError(f"Capture file is empty: {raw_path}")
            png_file = raw_path.with_suffix(".replay.png")
            debug = self.save_screen_debug(raw, raw_path.parent)
            Image.open(debug["decoded_path"]).save(png_file)
            self.last_raw = raw_path
            self.last_png = png_file
            self.log_msg("Loaded capture successfully")
            if debug_mode:
                self.log_msg(f"Debug capture decoded: {png_file}")
                self.log_screen_debug(debug)
                self.confirm_decoded_full(debug)
            self.show_image(png_file)
            self.finish_loaded_screen_preview(png_file)
        except Exception:
            raise

    def finish_loaded_screen_preview(self, image_path):
        self.set_progress(100, 100, "Saved screen loaded")
        self.ui_call(self.tabs.select, self.capture_tab)
        self.ui_call(self.status_var.set, f"Loaded saved screen capture: {Path(image_path).name}")
        self.log_msg(
            "Loaded saved screen capture into preview only. "
            "No waveform or power-quality analysis was run from this image."
        )

    def load_saved_waveform_capture(self, path):
        self.set_progress(text="Parsing waveform...", indeterminate=True)
        if not self.load_waveform(path):
            return
        self.compute_fft()
        self.log_msg("Analysis complete")
        self.set_progress(100, 100, "Analysis complete")

    def run_saved_screen_analysis(self, screen_path, debug_mode=False):
        self.log_msg("Parsing waveform...")
        self.ensure_session(reason="loaded saved capture")
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        export_dir = self.outdir / "reports" / f"loaded_capture_{ts}"
        export_dir.mkdir(parents=True, exist_ok=True)
        screen_target = export_dir / "screen_capture.png"
        if Path(screen_path).resolve() != screen_target.resolve():
            shutil.copyfile(screen_path, screen_target)
        self.last_png = screen_target
        report_text = self.visual_screen_analysis_fallback(
            export_dir,
            RuntimeError("Loaded saved capture uses screenshot analysis; no live QW serial session is available."),
        )
        self.build_professional_report(export_dir, self.instrument_id, "Loaded Saved Capture")
        self.register_report_session(export_dir, announce=True)
        self.ui_call(self.set_waveform_text, report_text)
        self.ui_call(self.waveform_status.set, "Analysis complete")
        self.set_progress(100, 100, "Analysis complete")
        self.log_msg("Analysis complete")

    def save_screen_debug(self, raw, output_dir):
        return save_screen_debug_files(
            raw,
            output_dir,
            expected_size=EXPECTED_SCREEN_SIZE,
            preview_max_size=self.preview_max_size(),
        )

    def preview_max_size(self):
        if hasattr(self, "image_canvas"):
            width = max(320, self.image_canvas.winfo_width() - 8)
            height = max(240, self.image_canvas.winfo_height() - 8)
        else:
            width, height = 900, 675
        return width, height

    def log_screen_debug(self, debug):
        self.log_msg(f"Screen source format: {debug['source_format']}")
        self.log_msg(f"Raw byte count received: {debug['raw_byte_count']}")
        self.log_msg(f"Decoded width/height: {debug['decoded_size'][0]} x {debug['decoded_size'][1]}")
        self.log_msg(f"Expected width/height: {debug['expected_size'][0]} x {debug['expected_size'][1]}")
        self.log_msg(f"Crop rectangle: {debug['crop_rect']}")
        self.log_msg(f"ESC/P raster band count: {debug.get('band_count', 'n/a')}")
        self.log_msg(f"ESC/P raster command counts: {debug.get('command_counts', {})}")
        self.log_msg(f"Preview max size: {debug.get('preview_max_size', 'n/a')}")
        self.log_msg(f"Preview scale factor: {debug.get('preview_scale', 'n/a')}")
        self.log_msg(f"Final rendered image size: {debug['rendered_size'][0]} x {debug['rendered_size'][1]}")
        self.log_msg(f"Debug raw: {debug['raw_path']}")
        self.log_msg(f"Debug decoded full: {debug['decoded_path']}")
        self.log_msg(f"Debug rendered preview: {debug['preview_path']}")
        self.log_msg(f"Decoded bottom LCD bar detected: {debug.get('decoded_bottom_status_detected', False)}")
        self.log_msg(f"Rendered preview bottom LCD bar detected: {debug.get('preview_bottom_status_detected', False)}")
        self.log_screen_capture_stage_result(debug)
        if debug["decoded_size"] == debug["expected_size"]:
            self.log_msg("Screen Capture Test PASS: full LCD image dimensions match expected display size.")
        else:
            self.log_msg("Screen Capture Test WARN: decoded size does not match expected full LCD dimensions.")

    def log_screen_capture_stage_result(self, debug):
        decoded_has_bottom = bool(debug.get("decoded_bottom_status_detected", False))
        preview_has_bottom = bool(debug.get("preview_bottom_status_detected", False))
        if not decoded_has_bottom:
            self.log_msg("BOTTOM LCD BAR LOST: decode stage")
        elif not preview_has_bottom:
            self.log_msg("BOTTOM LCD BAR LOST: preview generation stage")
        else:
            self.log_msg("Bottom LCD bar check PASS through decoded_full.png and rendered_preview.png")

    def confirm_decoded_full(self, debug):
        decoded_path = Path(debug["decoded_path"])
        preview_path = Path(debug["preview_path"])
        if not decoded_path.exists():
            self.log_msg(f"Decoded full image check FAIL: missing {decoded_path}")
            return
        with Image.open(decoded_path) as decoded_img:
            decoded_size = decoded_img.size
        self.log_msg(f"Decoded full image check: {decoded_path.name} is {decoded_size[0]} x {decoded_size[1]}")
        if preview_path.exists():
            with Image.open(preview_path) as preview_img:
                preview_size = preview_img.size
            self.log_msg(f"Rendered preview image check: {preview_path.name} is {preview_size[0]} x {preview_size[1]}")
        if decoded_size == tuple(debug["decoded_size"]):
            self.log_msg("Decoded full image check PASS: saved image matches decoded transfer.")
        else:
            self.log_msg("Decoded full image check WARN: saved image size differs from decoded transfer.")

    def show_image(self, path):
        source = Path(path)
        img = Image.open(source).convert("RGB")

        def update():
            self.image_full_ref = img.copy()
            self.image_preview_path = source
            self.render_screen_preview()

        self.ui_call(update)

    def on_screen_preview_resize(self, _event=None):
        if self.image_render_after_id is not None:
            try:
                self.root.after_cancel(self.image_render_after_id)
            except Exception:
                pass
        self.image_render_after_id = self.root.after(120, self.render_screen_preview)

    def render_screen_preview(self):
        self.image_render_after_id = None
        if self.image_full_ref is None or not hasattr(self, "image_canvas"):
            return

        canvas_w = max(1, self.image_canvas.winfo_width())
        canvas_h = max(1, self.image_canvas.winfo_height())
        full_w, full_h = self.image_full_ref.size
        if canvas_w < 20 or canvas_h < 20:
            self.image_render_after_id = self.root.after(200, self.render_screen_preview)
            return
        fit_w = max(1, canvas_w - 8)
        fit_h = max(1, canvas_h - 8)
        scale = min(fit_w / full_w, fit_h / full_h)
        scale = max(0.05, min(scale, 3.0))
        rendered_w = max(1, int(round(full_w * scale)))
        rendered_h = max(1, int(round(full_h * scale)))

        rendered = self.image_full_ref.resize((rendered_w, rendered_h), Image.Resampling.LANCZOS)
        preview_file = None
        if self.image_preview_path is not None:
            preview_file = self.image_preview_path.parent / "rendered_preview.png"
            try:
                rendered.save(preview_file)
            except Exception as exc:
                self.log_msg(f"Screen preview debug save failed: {exc}")

        photo = ImageTk.PhotoImage(rendered)
        self.image_ref = photo
        self.image_canvas.delete("all")
        x = max(0, (canvas_w - rendered_w) // 2)
        y = max(0, (canvas_h - rendered_h) // 2)
        self.image_canvas.create_image(x, y, image=self.image_ref, anchor="nw")
        self.image_canvas.configure(scrollregion=(0, 0, max(canvas_w, rendered_w), max(canvas_h, rendered_h)))

        self.log_msg(f"Screen preview decoded image size: {full_w} x {full_h}")
        self.log_msg(f"Screen preview area size: {fit_w} x {fit_h}")
        self.log_msg(f"Screen preview scale factor: {scale:.4f}")
        self.log_msg(f"Screen preview rendered size: {rendered_w} x {rendered_h}")
        self.log_msg(f"Screen preview Tk canvas/container size: {canvas_w} x {canvas_h}")
        self.log_msg("Screen preview crop rectangle: None")
        if bottom_status_region_detected(rendered):
            self.log_msg("Bottom LCD bar check PASS through Tk preview render")
        else:
            self.log_msg("BOTTOM LCD BAR LOST: Tk preview render stage")
        if preview_file:
            self.log_msg(f"Screen preview rendered file: {preview_file}")

    def open_last_image(self):
        if self.last_png and self.last_png.exists():
            self.open_path(self.last_png)
        else:
            messagebox.showinfo("No Image", "No captured image yet.")

    def copy_latest_screen_to_clipboard(self):
        if not self.last_png or not Path(self.last_png).exists():
            messagebox.showinfo("No Image", "No captured image yet.")
            return
        path = Path(self.last_png)
        try:
            if sys.platform.startswith("win"):
                self.copy_image_to_windows_clipboard(path)
                self.log_msg(f"Copied latest screen image to clipboard: {path.name}")
            else:
                self.root.clipboard_clear()
                self.root.clipboard_append(str(path))
                self.log_msg(f"Copied latest screen path to clipboard: {path}")
        except Exception as exc:
            self.log_msg(f"Clipboard copy failed: {exc}")
            self.show_error("Clipboard Error", exc)

    def copy_image_to_windows_clipboard(self, path):
        image = Image.open(path).convert("RGB")
        output = BytesIO()
        image.save(output, "BMP")
        dib = output.getvalue()[14:]
        output.close()

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        cf_dib = 8
        gmem_moveable = 0x0002

        if not user32.OpenClipboard(None):
            raise RuntimeError("Could not open Windows clipboard.")
        handle = None
        try:
            if not user32.EmptyClipboard():
                raise RuntimeError("Could not empty Windows clipboard.")
            handle = kernel32.GlobalAlloc(gmem_moveable, len(dib))
            if not handle:
                raise RuntimeError("Could not allocate clipboard memory.")
            locked = kernel32.GlobalLock(handle)
            if not locked:
                raise RuntimeError("Could not lock clipboard memory.")
            try:
                ctypes.memmove(locked, dib, len(dib))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(cf_dib, handle):
                raise RuntimeError("Could not set clipboard image data.")
            handle = None
        finally:
            user32.CloseClipboard()
            if handle:
                kernel32.GlobalFree(handle)

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
        safe_waveform = self.safe_199c_mode_var.get() and not self.advanced_transfer_mode_var.get()
        try:
            self.apply_calibration_settings(save=False)
            self.log_msg(f"Starting raw waveform download for trace {trace_no}...")
            self.set_progress(text="Waveform...", indeterminate=True)

            self.ensure_session(reason="raw waveform download")
            ts = time.strftime("%Y%m%d_%H%M%S")
            name = "A" if trace_no == "10" else "B" if trace_no == "20" else trace_no
            prefix = "fluke19x_safe_waveform" if safe_waveform else "fluke199c_waveform"
            raw_file = self.outdir / f"{prefix}_{name}_{ts}.bin"

            if safe_waveform:
                self.log_msg(
                    f"{SAFE_MODE_LABEL}: raw waveform download uses ID/QW only; "
                    f"no GR, no PC {WORK_BAUD}, no GL"
                )
                ser, client, ident = self.connect_safe_id_known_baud_recovery(xonxoff=False)
            else:
                ser, client, ident = self.connect_and_upgrade_baud()
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)
            self.log_msg(f"Active transfer baud: {client.baudrate}")

            from .waveform_protocol import query_waveform
            wf, raw = query_waveform(ser, client, trace_no)

            raw_file.write_bytes(raw)
            self.last_raw = raw_file
            self.last_waveform_raw = raw_file
            decoded = self.save_decoded_qw_outputs(wf, raw_file, trace_no)
            self.ui_call(self.waveform_status.set, f"Saved trace {trace_no}: {raw_file.name} ({len(raw)} bytes)")
            self.log_msg(f"Waveform raw saved: {raw_file}")
            self.log_msg(f"Waveform CSV saved: {decoded['csv_path']}")
            if decoded["plot_path"]:
                self.log_msg(f"Waveform preview plot saved: {decoded['plot_path']}")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Waveform Error", exc)
        finally:
            if ser is not None and client is not None:
                if safe_waveform:
                    self.close_serial_session_only(ser, f"{SAFE_MODE_LABEL} raw waveform download")
                else:
                    self.release_serial_session(ser, client)

    def save_decoded_qw_outputs(self, wf, raw_file, trace_no):
        from .waveform_protocol import save_single_waveform_plot, waveform_stats

        trace_name = "A" if trace_no == "10" else "B" if trace_no == "20" else str(trace_no)
        is_current = trace_no == "20"
        scale = CURRENT_SCALE_A_PER_V if is_current else 1.0
        value_label = "channel_b_current_a" if is_current else "channel_a_voltage_v"
        csv_path = raw_file.with_name(raw_file.stem + "_decoded.csv")
        plot_path = raw_file.with_name(raw_file.stem + "_preview.png")

        self.write_decoded_qw_csv(csv_path, wf, value_label, scale)
        plot_wf = dict(wf)
        plot_wf["y"] = [float(v) * scale for v in wf["y"]]
        plot_wf["y_unit"] = "A" if is_current else wf.get("y_unit", "V")
        if is_current:
            plotted = save_single_waveform_plot(plot_path, wf_b=plot_wf)
        else:
            plotted = save_single_waveform_plot(plot_path, wf_a=plot_wf)
        if plotted:
            self.last_fft = plot_path

        stats = waveform_stats(wf, scale=scale, log=self.log_msg, label=f"QW trace {trace_no}")
        duration = float((wf["n_points"] - 1) * wf["delta_x"]) if wf["n_points"] > 1 else 0.0
        lines = [
            f"QW TRACE {trace_no} DECODED",
            "===================",
            f"Trace source: {wf.get('trace_source', trace_name)}",
            f"Sample count: {wf['n_points']}",
            f"delta_x: {wf['delta_x']:.9g} {wf['x_unit']}",
            f"Duration: {duration:.9g} s",
            f"Y scale: {wf['y_scale']} {wf['y_unit']}/div",
            f"Y resolution: {wf['y_resolution']} {wf['y_unit']}/count",
            f"Units: X={wf['x_unit']} Y={plot_wf['y_unit']}",
            f"Sample width: {wf['sample_width']} bytes",
            f"Samples per point: {wf['samples_per_point']}",
            f"ADC min/max: {wf['adc_min']} / {wf['adc_max']}",
            f"Estimated frequency: {stats['frequency_hz']:.6g} Hz ({stats['frequency_method']})",
            f"Min/Max: {stats['min']:.6g} / {stats['max']:.6g} {plot_wf['y_unit']}",
            f"{'Irms' if is_current else 'Vrms'}: {stats['rms']:.6g} {plot_wf['y_unit']}",
            f"CSV: {csv_path.name}",
            f"Preview plot: {plot_path.name if plotted else 'Unavailable'}",
        ]
        text = "\n".join(lines)
        self.ui_call(self.set_waveform_text, text)
        for line in lines[2:15]:
            self.log_msg(f"QW decode: {line}")

        return {"csv_path": csv_path, "plot_path": plot_path if plotted else None, "stats": stats}

    def write_decoded_qw_csv(self, path, wf, value_label, scale):
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", value_label])
            for x, y in zip(wf["x"], wf["y"]):
                writer.writerow([float(x), float(y) * scale])

    def export_replay_thread(self):
        self.run_worker(self.export_replay_set)

    def analyze_full_capture_thread(self):
        self.run_worker(self.analyze_full_capture_deep_memory)

    def replay_progress(self, current, total, text):
        self.set_progress(current, total, text)
        self.ui_call(self.waveform_status.set, text)

    def connect_replay_serial(self):
        return self.connect_and_upgrade_baud()

    def export_replay_set(self):
        ser = None
        client = None
        try:
            self.apply_calibration_settings(save=False)
            self.log_msg("Starting deep-memory replay export...")
            self.set_progress(0, 100, "Replay 0%")
            self.ensure_session(reason="deep-memory replay export")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            export_dir = self.outdir / "reports" / ts
            export_dir.mkdir(parents=True, exist_ok=True)
            self.log_msg(f"Report output folder: {export_dir}")

            ser, client, ident = self.connect_replay_serial()
            self.instrument_id = ident
            self.ui_call(self.update_settings_display)
            self.log_msg(f"Active transfer baud: {client.baudrate}")

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

    def analyze_full_capture_deep_memory(self):
        try:
            self.apply_calibration_settings(save=False)
            report_dir = self.latest_report_dir or self.discover_latest_report_dir()
            if not report_dir:
                raise RuntimeError("No replay export folder available. Run Export Replay Set first, or choose a report folder.")
            report_dir = Path(report_dir)
            if not report_dir.exists():
                raise RuntimeError(f"Replay export folder not found: {report_dir}")
            replay_csvs = [p for p in report_dir.glob("replay_*_waveforms.csv") if p.name != "stitched_replay_waveforms.csv"]
            if not replay_csvs:
                raise RuntimeError("No replay frame waveform CSV files found. Run Export Replay Set first.")

            self.log_msg("Analyzing full capture deep memory reconstruction...")
            self.set_progress(text="Deep memory analysis...", indeterminate=True)
            from .waveform_protocol import analyze_deep_memory_capture
            outputs = analyze_deep_memory_capture(report_dir, log=self.log_msg)
            self.log_msg(f"Deep memory combined CSV: {outputs['full_csv']}")
            self.log_msg(f"Deep memory trends CSV: {outputs['trends_csv']}")
            self.log_msg(f"Deep memory summary: {outputs['summary_txt']}")
            self.register_report_session(report_dir, announce=True)
            self.tabs.select(self.reports_tab)
            self.refresh_reports_tab()
            self.set_progress(100, 100, "Deep memory analysis complete")
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Deep Memory Analysis Error", exc)

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
        if self.include_latest_screen_in_reports_var.get():
            screen_source = None
            if self.last_png and Path(self.last_png).exists():
                screen_source = Path(self.last_png)
            elif (self.outdir / "decoded_full.png").exists():
                screen_source = self.outdir / "decoded_full.png"

            if screen_source:
                shutil.copyfile(screen_source, screen_target)
                model_prefix = screen_capture_filename_prefix(self.instrument_id)
                named_target = report_dir / f"{model_prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png"
                shutil.copyfile(screen_source, named_target)
                self.log_msg(f"Registered latest screen image: {screen_target}")
                self.log_msg(f"Registered model-named screen image: {named_target}")
        else:
            self.log_msg("Report option skipped latest screen capture attachment.")

        self.register_report_session(report_dir, announce=True)

    def build_professional_report(self, report_dir, ident, report_type):
        try:
            self.write_job_metadata(report_dir, report_type)
            from .professional_report import build_professional_report_package
            build_professional_report_package(report_dir, ident, report_type, log=self.log_msg)
            html_file = next(Path(report_dir).glob("*.html"), None)
            txt_file = next(Path(report_dir).glob("*.txt"), None)
            self.verify_capture_files(html_file=html_file, txt_file=txt_file)
        except Exception as exc:
            self.log_msg(f"Professional report generation failed: {exc}")

    def write_job_metadata(self, report_dir, report_type):
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        notes = ""
        if hasattr(self, "generator_notes"):
            try:
                notes = self.generator_notes.get("1.0", "end").strip()
            except Exception:
                notes = ""
        metadata = "\n".join([
            f"Report type: {report_type}",
            f"Customer: {self.customer_var.get()}",
            f"Site: {self.site_var.get()}",
            f"Job name: {self.job_name_var.get()}",
            f"Unit ID: {self.generator_site_vars.get('generator_id').get() if self.generator_site_vars.get('generator_id') else ''}",
            f"Generator model: {self.generator_site_vars.get('engine_model').get() if self.generator_site_vars.get('engine_model') else ''}",
            f"Technician: {self.generator_site_vars.get('technician').get() if self.generator_site_vars.get('technician') else ''}",
            f"Date/time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "User notes:",
            notes,
        ])
        (report_dir / "job_metadata.txt").write_text(metadata, encoding="utf-8")
        with self.session_log_path().open("a", encoding="utf-8") as f:
            f.write("\n" + metadata + "\n")

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
        waveform_unavailable_report = report_dir / "WAVEFORM_REPORT_UNAVAILABLE.txt"
        generator_report = report_dir / "GENERATOR_COMMISSIONING_REPORT.html"
        autotune_report = report_dir / "AUTOTUNE_REPORT.html"
        is_single = single_report.exists() or report_dir.name.startswith("single_")
        is_waveform_unavailable = waveform_unavailable_report.exists()
        is_generator = generator_report.exists() or report_dir.name.startswith("generator_")
        is_autotune = autotune_report.exists() or report_dir.name.startswith("autotune_")
        is_fluke_connect = (report_dir / "FLUKE_CONNECT_IMPORT_SUMMARY.txt").exists()
        is_png_fallback = (report_dir / "ANALYZE_THIS_SCREEN.txt").exists() and self.row_is_png_fallback(self.read_summary_metrics(report_dir) or {})
        if is_waveform_unavailable:
            expected = [
                waveform_unavailable_report,
            ]
        elif is_fluke_connect:
            expected = [
                report_dir / "FLUKE_CONNECT_IMPORT_SUMMARY.txt",
                report_dir / "fluke_connect_measurements_normalized.csv",
            ]
        elif is_autotune:
            expected = [
                autotune_report,
                report_dir / "autotune_analysis.json",
                report_dir / "baseline_waveform.csv",
                report_dir / "load_step_waveform.csv",
                report_dir / "voltage_frequency_recovery.png",
                report_dir / "fft_harmonic_plot.png",
                report_dir / "before_after_gov_reg_settings.json",
            ]
        elif is_generator:
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
                report_dir / "PROFESSIONAL_REPORT.html",
                report_dir / "SUMMARY_METRICS.csv",
                report_dir / "waveform_samples.csv",
                report_dir / "waveform_plot.png",
                report_dir / "fft_spectrum.png",
                report_dir / "harmonic_summary.png",
            ]
            if (report_dir / "screen_capture.png").exists():
                expected.insert(1, report_dir / "screen_capture.png")
        elif is_png_fallback:
            expected = [
                report_dir / "ANALYZE_THIS_SCREEN.txt",
                report_dir / "screen_capture.png",
                report_dir / "SUMMARY_METRICS.csv",
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
                report_dir / "deep_memory_full_capture.csv",
                report_dir / "deep_memory_trends.csv",
                report_dir / "deep_memory_full_capture.png",
                report_dir / "deep_memory_trends.png",
                report_dir / "DEEP_MEMORY_SUMMARY.txt",
                report_dir / "deep_memory_worst_cases.csv",
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
            "autotune_html": autotune_report,
            "autotune_pdf": report_dir / "AUTOTUNE_REPORT.pdf",
            "autotune_json": report_dir / "autotune_analysis.json",
            "autotune_recovery_plot": report_dir / "voltage_frequency_recovery.png",
            "autotune_fft_plot": report_dir / "fft_harmonic_plot.png",
            "autotune_settings": report_dir / "before_after_gov_reg_settings.json",
            "final_report": report_dir / "FINAL_GLOBAL_REPORT.txt",
            "single_report": single_report,
            "waveform_unavailable_report": waveform_unavailable_report,
            "trend_plot": report_dir / "global_trend_summary.png",
            "harmonic_plot": report_dir / "global_harmonic_summary.png",
            "screen_capture": report_dir / "screen_capture.png",
            "waveform_export": report_dir / "waveform_export.csv",
            "stitched_csv": report_dir / "stitched_replay_waveforms.csv",
            "stitched_plot": report_dir / "stitched_replay_overview.png",
            "waterfall_plot": report_dir / "waterfall_replay_heatmap.png",
            "deep_memory_full_csv": report_dir / "deep_memory_full_capture.csv",
            "deep_memory_trends_csv": report_dir / "deep_memory_trends.csv",
            "deep_memory_worst_cases": report_dir / "deep_memory_worst_cases.csv",
            "deep_memory_summary": report_dir / "DEEP_MEMORY_SUMMARY.txt",
            "deep_memory_full_plot": report_dir / "deep_memory_full_capture.png",
            "deep_memory_trend_plot": report_dir / "deep_memory_trends.png",
            "fluke_connect_summary": report_dir / "FLUKE_CONNECT_IMPORT_SUMMARY.txt",
            "fluke_connect_csv": report_dir / "fluke_connect_measurements_normalized.csv",
            "fluke_connect_pdfs": sorted(report_dir.glob("*.pdf")),
            "single_waveform_plot": report_dir / "single_waveform_plot.png",
            "single_fft_plot": report_dir / "single_fft_plot.png",
            "csv_files": sorted(report_dir.glob("*.csv")),
        }

        missing = [p for p in expected if not p.exists()]
        registered = []
        seen_registered = set()
        for value in files.values():
            if isinstance(value, list):
                values = [p for p in value if p.exists()]
            elif value.exists():
                values = [value]
            else:
                values = []
            for path in values:
                key = Path(path).resolve()
                if key not in seen_registered:
                    registered.append(path)
                    seen_registered.add(key)

        self.latest_report_dir = report_dir
        self.report_files = files
        self.log_msg(f"Report output folder: {report_dir}")
        self.log_msg(f"Files registered in UI: {', '.join(p.name for p in registered) if registered else 'none'}")
        if missing:
            self.log_msg(f"Missing expected report files: {', '.join(p.name for p in missing)}")
        else:
            self.log_msg("Missing expected report files: none")

        self.ui_call(self.update_report_summary_card, report_dir)
        self.ui_call(self.refresh_reports_tab)
        if announce:
            self.surface_completed_report_package(report_dir, registered, missing)

    def surface_completed_report_package(self, report_dir, registered, missing):
        def update():
            title = "Report package complete"
            detail = f"{len(registered)} files registered for this session: {report_dir}"
            is_png_fallback = self.row_is_png_fallback(self.read_summary_metrics(report_dir) or {})
            if is_png_fallback:
                title = "Image-only fallback report completed."
                detail = f"PNG fallback only: screen_capture.png, ANALYZE_THIS_SCREEN.txt, SUMMARY_METRICS.csv | {report_dir}"
            elif (Path(report_dir) / "FLUKE_CONNECT_IMPORT_SUMMARY.txt").exists():
                title = "Fluke Connect import completed."
                detail = f"FLUKE_CONNECT_IMPORT_SUMMARY.txt, fluke_connect_measurements_normalized.csv | {report_dir}"
            elif (Path(report_dir) / "WAVEFORM_REPORT_UNAVAILABLE.txt").exists():
                title = "Waveform report unavailable."
                detail = f"WAVEFORM_REPORT_UNAVAILABLE.txt | {report_dir}"
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

            self.preview_preferred_report_file()
            self.status_var.set("Report package complete - available in Reports")

        self.ui_call(update)

    def preview_preferred_report_file(self):
        preferred = [
            self.report_files.get("professional_waveform_plot"),
            self.report_files.get("single_waveform_plot"),
            self.report_files.get("trend_plot"),
            self.report_files.get("screen_capture"),
            self.report_files.get("professional_html"),
            self.report_files.get("single_report"),
            self.report_files.get("final_report"),
        ]
        for path in preferred:
            if path and Path(path).exists():
                if hasattr(self, "report_listbox"):
                    for idx, item in enumerate(self.report_file_items):
                        if Path(item) == Path(path):
                            self.report_listbox.selection_clear(0, "end")
                            self.report_listbox.selection_set(idx)
                            self.report_listbox.activate(idx)
                            break
                self.preview_report_file(path)
                return
        self.preview_first_report_file()

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
        if not report_dir.exists():
            self.set_report_text_preview("Missing report folder", f"Report folder not found:\n{report_dir}")
            if hasattr(self, "report_listbox"):
                self.report_listbox.delete(0, "end")
            if hasattr(self, "report_status_var"):
                self.report_status_var.set(f"Missing report bundle: {report_dir}")
            return
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
            "autotune_html": report_dir / "AUTOTUNE_REPORT.html",
            "autotune_pdf": report_dir / "AUTOTUNE_REPORT.pdf",
            "autotune_json": report_dir / "autotune_analysis.json",
            "autotune_recovery_plot": report_dir / "voltage_frequency_recovery.png",
            "autotune_fft_plot": report_dir / "fft_harmonic_plot.png",
            "autotune_settings": report_dir / "before_after_gov_reg_settings.json",
            "final_report": report_dir / "FINAL_GLOBAL_REPORT.txt",
            "single_report": report_dir / "SINGLE_WAVEFORM_REPORT.txt",
            "trend_plot": report_dir / "global_trend_summary.png",
            "harmonic_plot": report_dir / "global_harmonic_summary.png",
            "screen_capture": report_dir / "screen_capture.png",
            "waveform_export": report_dir / "waveform_export.csv",
            "stitched_csv": report_dir / "stitched_replay_waveforms.csv",
            "stitched_plot": report_dir / "stitched_replay_overview.png",
            "waterfall_plot": report_dir / "waterfall_replay_heatmap.png",
            "deep_memory_full_csv": report_dir / "deep_memory_full_capture.csv",
            "deep_memory_trends_csv": report_dir / "deep_memory_trends.csv",
            "deep_memory_worst_cases": report_dir / "deep_memory_worst_cases.csv",
            "deep_memory_summary": report_dir / "DEEP_MEMORY_SUMMARY.txt",
            "deep_memory_full_plot": report_dir / "deep_memory_full_capture.png",
            "deep_memory_trend_plot": report_dir / "deep_memory_trends.png",
            "single_waveform_plot": report_dir / "single_waveform_plot.png",
            "single_fft_plot": report_dir / "single_fft_plot.png",
            "csv_files": sorted(report_dir.glob("*.csv")),
        }
        all_files = sorted(p for p in report_dir.rglob("*") if p.is_file())
        if hasattr(self, "report_status_var"):
            self.report_status_var.set(f"Latest report bundle: {report_dir}")
        if hasattr(self, "report_package_title_var"):
            self.report_package_title_var.set("Report package loaded")
            self.report_package_detail_var.set(f"Viewing report package: {report_dir}")

        if hasattr(self, "report_listbox"):
            self.report_listbox.delete(0, "end")
            self.report_file_items = []
            self.report_file_meta = []
            entries = self.categorize_report_files(report_dir, all_files)
            for label, path, category, advanced in entries:
                if advanced and not self.report_show_advanced_var.get():
                    continue
                self.report_file_items.append(path)
                self.report_file_meta.append({"category": category, "advanced": advanced})
                self.report_listbox.insert("end", label)

        self.preview_first_report_file()

    def categorize_report_files(self, report_dir, files):
        report_dir = Path(report_dir)
        priority_names = {
            "FINAL_GLOBAL_REPORT.txt": ("Report", 0),
            "PROFESSIONAL_REPORT.html": ("Report", 1),
            "SINGLE_WAVEFORM_REPORT.txt": ("Report", 2),
            "WAVEFORM_REPORT_UNAVAILABLE.txt": ("Report", 2),
            "GENERATOR_COMMISSIONING_REPORT.html": ("Report", 3),
            "AUTOTUNE_REPORT.html": ("Report", 4),
            "replay_summary.csv": ("Summary CSV", 10),
            "SUMMARY_METRICS.csv": ("Summary CSV", 11),
            "waveform_export.csv": ("Waveform CSV", 20),
            "waveform_samples.csv": ("Waveform CSV", 21),
            "baseline_waveform.csv": ("Waveform CSV", 22),
            "load_step_waveform.csv": ("Waveform CSV", 23),
            "global_trend_summary.png": ("Plot", 30),
            "global_harmonic_summary.png": ("Plot", 31),
            "fft_spectrum.png": ("FFT Plot", 32),
            "fft_harmonic_plot.png": ("FFT Plot", 33),
            "harmonic_summary.png": ("Harmonic Plot", 34),
            "stitched_replay_overview.png": ("Stitched Replay", 35),
            "waterfall_replay_heatmap.png": ("Stitched Replay", 36),
            "deep_memory_full_capture.png": ("Deep Memory", 37),
            "deep_memory_trends.png": ("Deep Memory", 38),
            "DEEP_MEMORY_SUMMARY.txt": ("Deep Memory", 39),
            "deep_memory_full_capture.csv": ("Deep Memory CSV", 23),
            "deep_memory_trends.csv": ("Deep Memory CSV", 24),
            "deep_memory_worst_cases.csv": ("Deep Memory CSV", 25),
            "screen_capture.png": ("Screenshot", 40),
        }
        image_ext = {".png", ".jpg", ".jpeg", ".bmp"}
        text_ext = {".txt", ".html", ".htm", ".csv", ".json"}
        raw_ext = {".bin", ".raw", ".dat"}

        entries = []
        seen = set()
        for path in files:
            path = Path(path)
            name = path.name
            ext = path.suffix.lower()
            category, priority = priority_names.get(name, ("Advanced", 900))
            advanced = False
            if name not in priority_names:
                lower = name.lower()
                if ext == ".csv":
                    category = "Waveform CSV" if self.is_waveform_csv(path) else "CSV"
                    priority = 24 if category == "Waveform CSV" else 60
                elif ext in image_ext:
                    if "fft" in lower:
                        category, priority = "FFT Plot", 32
                    elif "harmonic" in lower:
                        category, priority = "Harmonic Plot", 34
                    elif "screen" in lower or "capture" in lower:
                        category, priority = "Screenshot", 40
                    else:
                        category, priority = "Plot", 39
                elif ext in text_ext:
                    category, priority = "Report", 55
                elif ext in raw_ext or "debug" in lower or "raw" in lower:
                    category, priority, advanced = "Advanced", 900, True
                else:
                    category, priority, advanced = "Advanced", 910, True
            if path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(report_dir) if path.is_relative_to(report_dir) else path.name
            label_prefix = "Advanced" if advanced else category
            label = f"{label_prefix} | {rel}"
            if self.row_is_png_fallback(self.read_summary_metrics(report_dir) or {}) and name in {
                "screen_capture.png",
                "ANALYZE_THIS_SCREEN.txt",
                "SUMMARY_METRICS.csv",
            }:
                label = str(rel)
            entries.append((priority, str(rel).lower(), label, path, category, advanced))

        entries.sort(key=lambda item: (item[0], item[1]))
        return [(label, path, category, advanced) for _priority, _sort, label, path, category, advanced in entries]

    def is_waveform_csv(self, path):
        try:
            with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as f:
                header = next(csv.reader(f), [])
        except Exception:
            return False
        fields = [h.strip().lower() for h in header]
        has_time = any("time" in h or h in ("x", "sec", "seconds") for h in fields)
        has_voltage = any("voltage" in h or "channel a" in h or "ch_a" in h or h.endswith("_a") for h in fields)
        has_current = any("current" in h or "channel b" in h or "ch_b" in h or h.endswith("_b") for h in fields)
        return has_time and (has_voltage or has_current)

    def preview_first_report_file(self):
        if not hasattr(self, "report_listbox") or self.report_listbox.size() == 0:
            if hasattr(self, "report_text"):
                self.set_report_text_preview("No previewable report files", "No report files were found in this package.")
            return
        if not self.report_listbox.curselection():
            self.report_listbox.selection_set(0)
            self.report_listbox.activate(0)
        self.preview_report_file(self.selected_report_path())

    def open_selected_report_in_app(self):
        path = self.selected_report_path()
        if not path:
            return
        self.preview_report_file(path)

    def set_report_text_preview(self, title, content):
        self.report_preview_title_var.set(title)
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("end", content)
        self.report_text.configure(state="disabled")
        self.report_text.tkraise()

    def set_report_canvas_preview(self, title):
        self.report_preview_title_var.set(title)
        self.report_canvas.delete("all")
        tk.Misc.tkraise(self.report_canvas)

    def preview_report_file(self, path, preserve_mode=False):
        if not path or not hasattr(self, "report_text"):
            return
        path = Path(path)
        if not path.exists():
            self.set_report_text_preview("Missing file", f"File not found:\n{path}")
            return

        ext = path.suffix.lower()
        try:
            if ext in {".png", ".jpg", ".jpeg", ".bmp"}:
                self.preview_image_file(path)
            elif ext == ".csv" and self.is_waveform_csv(path):
                self.preview_waveform_csv(path)
            elif ext == ".csv":
                self.preview_text_table_file(path)
            elif ext in {".txt", ".html", ".htm", ".json"}:
                self.preview_text_table_file(path)
            elif ext == ".pdf":
                self.set_report_text_preview(
                    f"PDF Preview: {path.name}",
                    "PDF was generated successfully. In-app PDF rendering is not bundled to keep the field-tablet app lightweight.\n\n"
                    "Use Open External only if you need to inspect or print this PDF with Windows.",
                )
            else:
                self.set_report_text_preview(
                    f"Advanced File: {path.name}",
                    f"This file is saved in the report package for troubleshooting or archival use.\n\n{path}",
                )
        except Exception as exc:
            self.set_report_text_preview(f"Preview Error: {path.name}", f"Could not preview {path}:\n{exc}")

    def preview_image_file(self, path):
        title = "Waveform Image Preview" if "waveform" in path.name.lower() else "Image Preview"
        self.set_report_canvas_preview(f"{title}: {path.name}")
        self.root.update_idletasks()
        container_w = max(1, self.report_canvas.winfo_width())
        container_h = max(1, self.report_canvas.winfo_height())
        img = Image.open(path).convert("RGB")
        original_w, original_h = img.size
        fit_w = max(1, container_w - 8)
        fit_h = max(1, container_h - 8)
        scale = min(fit_w / original_w, fit_h / original_h)
        if scale <= 0 or not math.isfinite(scale):
            scale = 1.0
        scaled_w = max(1, int(round(original_w * scale)))
        scaled_h = max(1, int(round(original_h * scale)))
        rendered = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

        self.report_preview_image_ref = ImageTk.PhotoImage(rendered)
        x = max(0, (container_w - scaled_w) // 2)
        y = max(0, (container_h - scaled_h) // 2)
        self.report_canvas.create_image(x, y, image=self.report_preview_image_ref, anchor="nw")
        self.report_canvas.configure(scrollregion=(0, 0, max(container_w, scaled_w), max(container_h, scaled_h)))

        self.log_msg(f"Analyzer image preview original image size: {original_w} x {original_h}")
        self.log_msg(f"Analyzer image preview container width/height: {container_w} x {container_h}")
        self.log_msg(f"Analyzer image preview scaled image size: {scaled_w} x {scaled_h}")
        self.log_msg(f"Analyzer image preview final widget size: {self.report_canvas.winfo_width()} x {self.report_canvas.winfo_height()}")
        self.log_msg("Analyzer image preview crop rectangle: None")
        if bottom_status_region_detected(rendered):
            self.log_msg("Bottom LCD bar check PASS through Analyzer preview render")
        else:
            self.log_msg("BOTTOM LCD BAR LOST: Analyzer preview render stage")

    def open_large_preview(self):
        path = self.selected_report_path()
        if not path or not Path(path).exists():
            messagebox.showinfo("No Report File", "Select a PNG/JPG/BMP plot or screen image first.")
            return
        path = Path(path)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
            messagebox.showinfo("Large Preview", "Large preview is available for image and plot files.")
            return

        window = tk.Toplevel(self.root)
        window.title(f"Large Preview - {path.name}")
        window.geometry("1100x780")
        window.minsize(720, 480)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text=path.name, font=("Arial", 16, "bold"), padding=10).grid(row=0, column=0, sticky="ew")
        canvas = tk.Canvas(window, bg="white", highlightthickness=0)
        canvas.grid(row=1, column=0, sticky="nsew")
        vscroll = ttk.Scrollbar(window, orient="vertical", command=canvas.yview)
        vscroll.grid(row=1, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(window, orient="horizontal", command=canvas.xview)
        hscroll.grid(row=2, column=0, sticky="ew")
        canvas.configure(xscrollcommand=hscroll.set, yscrollcommand=vscroll.set)
        image = Image.open(path).convert("RGB")
        photo_ref = {"photo": None}

        def render(_event=None):
            width = max(1, canvas.winfo_width())
            height = max(1, canvas.winfo_height())
            scale = min(width / image.width, height / image.height)
            scale = min(scale, 2.0)
            rendered_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            rendered = image.resize(rendered_size, Image.Resampling.LANCZOS)
            photo_ref["photo"] = ImageTk.PhotoImage(rendered)
            canvas.delete("all")
            x = max(0, (width - rendered_size[0]) // 2)
            y = max(0, (height - rendered_size[1]) // 2)
            canvas.create_image(x, y, image=photo_ref["photo"], anchor="nw")
            canvas.configure(scrollregion=(0, 0, max(width, rendered_size[0]), max(height, rendered_size[1])))

        canvas.bind("<Configure>", render)
        render()

    def preview_text_table_file(self, path):
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in {".html", ".htm"}:
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
            text = re.sub(r"(?s)<br\s*/?>", "\n", text)
            text = re.sub(r"(?s)</(p|div|tr|h[1-6]|li|table)>", "\n", text)
            text = re.sub(r"(?s)<.*?>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s+\n", "\n\n", text)
            content = text.strip()
        elif path.suffix.lower() == ".csv":
            rows = []
            with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.reader(f)
                for idx, row in enumerate(reader):
                    rows.append(" | ".join(cell.strip() for cell in row))
                    if idx >= 250:
                        rows.append("... preview truncated ...")
                        break
            content = "\n".join(rows)
        else:
            content = raw
        if len(content) > 120000:
            content = content[:120000] + "\n\n... preview truncated ..."
        self.set_report_text_preview(f"Text Preview: {path.name}", content or "(empty file)")

    def preview_waveform_csv(self, path):
        rows = []
        with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            for row in reader:
                rows.append(row)
                if len(rows) >= 5000:
                    break
        if not rows:
            self.set_report_text_preview(f"Waveform CSV: {path.name}", "CSV file has no waveform rows.")
            return

        def pick_field(candidates):
            lowered = {name.lower(): name for name in fields}
            for token in candidates:
                for low, original in lowered.items():
                    if token in low:
                        return original
            return None

        time_field = pick_field(["time", "seconds", "sec"]) or fields[0]
        voltage_field = pick_field(["voltage", "channel a", "ch_a", "trace_a", "_a"])
        current_field = pick_field(["current", "channel b", "ch_b", "trace_b", "_b"])
        if not voltage_field and len(fields) > 1:
            voltage_field = fields[1]
        if not current_field and len(fields) > 2:
            current_field = fields[2]

        def parse_series(field):
            vals = []
            for row in rows:
                try:
                    vals.append(float(row.get(field, "")))
                except Exception:
                    vals.append(float("nan"))
            return vals

        t = parse_series(time_field)
        voltage = parse_series(voltage_field) if voltage_field else []
        current = parse_series(current_field) if current_field else []
        self.draw_waveform_preview(path.name, t, voltage, current, voltage_field, current_field)

    def draw_waveform_preview(self, title, t, voltage, current, voltage_label, current_label):
        self.set_report_canvas_preview(f"Waveform CSV Preview: {title}")
        width = max(320, self.report_canvas.winfo_width())
        height = max(260, self.report_canvas.winfo_height())
        pad_l, pad_r, pad_t, pad_b = 64, 24, 28, 42
        plot_w = max(1, width - pad_l - pad_r)
        plot_h = max(1, height - pad_t - pad_b)

        def finite_pairs(y):
            pairs = []
            for tx, yy in zip(t, y):
                try:
                    if math.isfinite(tx) and math.isfinite(yy):
                        pairs.append((tx, yy))
                except Exception:
                    pass
            return pairs

        series = [
            ("Voltage", finite_pairs(voltage), "#0b5cab"),
            ("Current", finite_pairs(current), "#b04700"),
        ]
        line_width = 3 if getattr(self, "field_mode_var", tk.StringVar(value="")).get() == "Sunlight" else 2
        all_t = [x for _name, pairs, _color in series for x, _y in pairs]
        if not all_t:
            self.report_canvas.create_text(width // 2, height // 2, text="No numeric waveform data found.", fill="#333333")
            return

        x_min, x_max = min(all_t), max(all_t)
        if x_min == x_max:
            x_max = x_min + 1.0
        self.report_canvas.create_rectangle(pad_l, pad_t, width - pad_r, height - pad_b, outline="#888888")
        self.report_canvas.create_text(pad_l, 8, text=title, anchor="nw", fill="#111111", font=("Arial", 12, "bold"))
        self.report_canvas.create_text(width // 2, height - 12, text="Time", fill="#333333")

        for idx, (name, pairs, color) in enumerate(series):
            if len(pairs) < 2:
                continue
            y_vals = [y for _x, y in pairs]
            y_min, y_max = min(y_vals), max(y_vals)
            if y_min == y_max:
                y_min -= 1.0
                y_max += 1.0
            points = []
            max_points = 1200
            step = max(1, len(pairs) // max_points)
            for x, y in pairs[::step]:
                px = pad_l + ((x - x_min) / (x_max - x_min)) * plot_w
                py = pad_t + (1.0 - ((y - y_min) / (y_max - y_min))) * plot_h
                if math.isfinite(px) and math.isfinite(py):
                    px = min(max(px, pad_l), width - pad_r)
                    py = min(max(py, pad_t), height - pad_b)
                    points.extend((px, py))
            if len(points) >= 4:
                self.report_canvas.create_line(*points, fill=color, width=line_width)
            label = f"{name}: {voltage_label if name == 'Voltage' else current_label}"
            self.report_canvas.create_text(width - pad_r - 8, pad_t + 18 + idx * 20, text=label, anchor="ne", fill=color, font=("Arial", 10, "bold"))

    def read_summary_metrics(self, report_dir=None):
        report_dir = Path(report_dir or self.latest_report_dir) if (report_dir or self.latest_report_dir) else None
        if not report_dir:
            return None
        path = report_dir / "SUMMARY_METRICS.csv"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
        return rows[0]

    def update_report_summary_card(self, report_dir=None):
        row = self.read_summary_metrics(report_dir)
        if not hasattr(self, "report_summary_vars"):
            return
        if not row:
            for var in self.report_summary_vars.values():
                var.set("Not captured")
            return
        self.report_summary_vars["vrms_v"].set(self.summary_display(row, "vrms_v", 2, " V"))
        self.report_summary_vars["irms_a"].set(self.summary_display(row, "irms_a", 2, " A"))
        self.report_summary_vars["frequency_hz"].set(self.summary_display(row, "frequency_hz", 2, " Hz"))
        thd_v = self.summary_display(row, "thd_v_percent", 2, " %")
        thd_i = self.summary_display(row, "thd_i_percent", 2, " %")
        if self.row_is_png_fallback(row):
            self.report_summary_vars["thd"].set(thd_i if "estimated" in thd_i.lower() else "Moderate (estimated)")
        else:
            self.report_summary_vars["thd"].set(f"V {thd_v} / I {thd_i}")
        self.report_summary_vars["power_factor"].set(self.summary_display(row, "power_factor", 3, ""))

    def row_is_png_fallback(self, row):
        source = " ".join([
            row.get("report_source", ""),
            row.get("frame_name", ""),
            row.get("confidence", ""),
        ]).lower()
        return "png" in source or "image mode" in " ".join(row.values()).lower()

    def summary_display(self, row, key, digits=2, suffix=""):
        raw = str(row.get(key, "")).strip()
        if not raw:
            return "Not captured"
        try:
            value = float(raw)
            if math.isfinite(value):
                return f"{value:.{digits}f}{suffix}"
        except Exception:
            pass
        lowered = raw.lower()
        if lowered in ("nan", "none", "null", "--"):
            return "Unavailable"
        return raw

    def summary_value(self, row, key, digits=2):
        try:
            value = float(row.get(key, ""))
            if math.isfinite(value):
                return f"{value:.{digits}f}"
        except Exception:
            pass
        raw = str(row.get(key, "")).strip()
        return raw if raw else "Not captured"

    def clean_summary_text(self):
        row = self.read_summary_metrics()
        if row:
            if self.row_is_png_fallback(row):
                return "\n".join([
                    f"{APP_NAME} - Summary",
                    f"Scope: {row.get('scope_id', self.instrument_id)}",
                    f"Report: {row.get('report_type', 'PNG Screen Analysis')}",
                    f"Timestamp: {row.get('timestamp', time.strftime('%Y-%m-%d %H:%M:%S'))}",
                    "",
                    "Source: PNG Screen Analysis (Reduced Confidence)",
                    f"Vrms: {self.summary_display(row, 'vrms_v')}",
                    f"Irms: {self.summary_display(row, 'irms_a')}",
                    f"Frequency: {self.summary_display(row, 'frequency_hz')}",
                    f"Power Factor: {self.summary_display(row, 'power_factor')}",
                    f"THD: {self.summary_display(row, 'thd_i_percent')}",
                    f"Waveform: {row.get('waveform', 'Nonlinear current load')}",
                    f"Voltage quality: {row.get('voltage_quality', 'Normal')}",
                    "",
                    "Measurement source: screenshot fallback; numeric QW waveform data unavailable.",
                ])
            return "\n".join([
                f"{APP_NAME} - Summary",
                f"Scope: {row.get('scope_id', self.instrument_id)}",
                f"Report: {row.get('report_type', 'Report')}",
                f"Timestamp: {row.get('timestamp', time.strftime('%Y-%m-%d %H:%M:%S'))}",
                "",
                f"Vrms: {self.summary_value(row, 'vrms_v', 2)} V",
                f"Irms: {self.summary_value(row, 'irms_a', 2)} A",
                f"Frequency: {self.summary_value(row, 'frequency_hz', 2)} Hz",
                f"Real Power: {self.summary_value(row, 'real_power_kw', 2)} kW",
                f"Apparent Power: {self.summary_value(row, 'apparent_power_kva', 2)} kVA",
                f"Reactive Power: {self.summary_value(row, 'reactive_power_kvar', 2)} kVAR",
                f"Power Factor: {self.summary_value(row, 'power_factor', 3)}",
                f"THD-V: {self.summary_value(row, 'thd_v_percent', 2)} %",
                f"THD-I: {self.summary_value(row, 'thd_i_percent', 2)} %",
                "",
                "Measurement source: QW numeric waveform data, not screenshot pixels.",
            ])

        for key in ("single_report", "final_report"):
            path = self.report_files.get(key)
            if path and Path(path).exists():
                return Path(path).read_text(encoding="utf-8", errors="replace")[:4000]
        return None

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

    def open_selected_report_external(self):
        path = self.selected_report_path()
        if not path:
            messagebox.showinfo("No Report File", "Select a report file first.")
            return
        self.open_path_external(path)

    def open_selected_report_file(self):
        self.open_selected_report_external()

    def save_selected_report_as(self):
        path = self.selected_report_path()
        if not path:
            messagebox.showinfo("No Report File", "Select a report file first.")
            return
        target = filedialog.asksaveasfilename(initialfile=path.name)
        if target:
            try:
                shutil.copyfile(path, target)
                self.log_msg(f"Saved report file as: {target}")
            except Exception as exc:
                self.log_msg(f"Report Save As failed: {exc}")
                self.show_error("Report Save Error", exc)

    def choose_report_folder(self):
        initial = str(self.latest_report_dir or self.outdir / "reports")
        folder = filedialog.askdirectory(initialdir=initial, title="Choose Report Folder")
        if not folder:
            return
        report_dir = Path(folder)
        if not report_dir.exists():
            messagebox.showinfo("Report Folder", "Selected folder does not exist.")
            return
        self.register_report_session(report_dir, announce=True)
        self.tabs.select(self.reports_tab)
        self.preview_first_report_file()

    def export_report_folder_external(self):
        report_dir = self.latest_report_dir or self.discover_latest_report_dir()
        if report_dir and Path(report_dir).exists():
            self.open_path_external(report_dir)
        else:
            messagebox.showinfo("No Report", "No report bundle has been generated yet.")

    def open_professional_report(self):
        path = self.report_files.get("professional_html") or self.report_files.get("generator_html") or self.report_files.get("autotune_html")
        if (not path or not Path(path).exists()) and self.report_files.get("generator_html"):
            path = self.report_files.get("generator_html")
        if (not path or not Path(path).exists()) and self.report_files.get("autotune_html"):
            path = self.report_files.get("autotune_html")
        if path and Path(path).exists():
            self.tabs.select(self.reports_tab)
            self.preview_report_file(path)
        else:
            messagebox.showinfo("No Professional Report", "No HTML report is available for this package.")

    def export_professional_pdf(self):
        path = self.report_files.get("professional_pdf") or self.report_files.get("generator_pdf") or self.report_files.get("autotune_pdf")
        if (not path or not Path(path).exists()) and self.report_files.get("generator_pdf"):
            path = self.report_files.get("generator_pdf")
        if (not path or not Path(path).exists()) and self.report_files.get("autotune_pdf"):
            path = self.report_files.get("autotune_pdf")
        if path and Path(path).exists():
            self.tabs.select(self.reports_tab)
            self.preview_report_file(path)
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
                self.tabs.select(self.reports_tab)
                self.preview_report_file(path)
                return

        messagebox.showinfo("PDF Unavailable", "PDF generator is not available, or no professional PDF could be produced.")

    def copy_summary_to_clipboard(self):
        clean_text = self.clean_summary_text()
        if not clean_text:
            messagebox.showinfo("No Summary", "No summary metrics are available for this report package.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(clean_text)
        self.log_msg("Copied clean executive summary to clipboard")

    def copy_html_to_clipboard(self):
        path = self.report_files.get("professional_html") or self.report_files.get("generator_html") or self.report_files.get("autotune_html")
        if not path or not Path(path).exists():
            messagebox.showinfo("No HTML", "No HTML report is available for this package.")
            return
        try:
            html_text = Path(path).read_text(encoding="utf-8", errors="replace")
            self.root.clipboard_clear()
            self.root.clipboard_append(html_text)
            self.log_msg(f"Copied HTML report source to clipboard: {Path(path).name}")
        except Exception as exc:
            self.log_msg(f"Copy HTML failed: {exc}")
            self.show_error("Copy HTML Error", exc)

    def print_selected_report(self):
        path = self.selected_report_path()
        if not path:
            messagebox.showinfo("No Report File", "Select a report file first.")
            return
        if sys.platform.startswith("win"):
            try:
                os.startfile(str(path), "print")
                self.log_msg(f"Sent to Windows print handler: {path}")
            except Exception as exc:
                messagebox.showerror("Print Error", str(exc))
        else:
            self.open_path_external(path)

    def open_stitched_replay_view(self):
        path = self.report_files.get("stitched_plot") or self.report_files.get("stitched_csv")
        if path and Path(path).exists():
            self.open_path(path)
        else:
            messagebox.showinfo("No Stitched Replay View", "No stitched replay view is available for this report package.")

    def open_waterfall_replay_view(self):
        path = self.report_files.get("waterfall_plot")
        if path and Path(path).exists():
            self.open_path(path)
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
                self.open_path(path)
                return

        messagebox.showinfo("No FFT Plot", "No FFT plot is available yet. Generate a waveform report or compute FFT first.")

    def open_last_report(self):
        report_dir = self.latest_report_dir or self.discover_latest_report_dir()
        if report_dir and Path(report_dir).exists():
            if self.latest_report_dir != Path(report_dir):
                self.register_report_session(Path(report_dir))
            self.tabs.select(self.reports_tab)
            self.refresh_reports_tab()
            self.preview_first_report_file()
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

    def choose_fluke_connect_folder(self):
        folder = filedialog.askdirectory(
            initialdir=str(Path(self.fluke_connect_inbox_var.get() or DEFAULT_FLUKE_CONNECT_INBOX)),
            title="Choose Fluke Connect inbox folder",
        )
        if folder:
            self.fluke_connect_inbox_var.set(folder)
            self.log_msg(f"Fluke Connect inbox selected: {folder}")

    def import_fluke_connect_thread(self):
        self.run_worker(self.import_fluke_connect_inbox)

    def import_fluke_connect_inbox(self):
        try:
            source = Path(self.fluke_connect_inbox_var.get() or DEFAULT_FLUKE_CONNECT_INBOX)
            self.ensure_session(reason="Fluke Connect import")
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            output_dir = self.outdir / "reports" / f"fluke_connect_import_{ts}"
            self.log_msg(f"Importing Fluke Connect inbox: {source}")
            self.set_progress(text="Importing Fluke Connect...", indeterminate=True)
            result = import_fluke_connect_folder(source, output_dir)
            self.latest_report_dir = output_dir
            self.register_report_session(output_dir, announce=True)
            self.tabs.select(self.reports_tab)
            self.refresh_reports_tab()
            self.preview_report_file(result["summary_txt"])
            self.set_progress(100, 100, "Fluke Connect import complete")
            self.log_msg(
                "Fluke Connect import complete: "
                f"{len(result['csv_files'])} CSV, {len(result['pdf_files'])} PDF, "
                f"{result['summary']['rows']} measurement rows"
            )
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Fluke Connect Import Error", exc)

    def ask_waveform_path(self):
        path = filedialog.askopenfilename(
            initialdir=str(self.outdir),
            title="Load raw waveform",
            filetypes=(("Raw waveform", "*.bin"), ("All files", "*.*")),
        )
        return Path(path) if path else None

    def load_waveform(self, path):
        try:
            path = Path(path)
            if not path.exists():
                raise FileNotFoundError(f"Waveform file not found: {path}")
            size = path.stat().st_size
            if size < 8:
                raise RuntimeError(f"Waveform file is too small or corrupted ({size} bytes): {path.name}")
            self.last_waveform_raw = path
            self.last_raw = path
            self.ui_call(self.waveform_status.set, f"Loaded waveform: {path.name} ({size} bytes)")
            self.log_msg(f"Loaded waveform raw: {path}")
            self.log_msg("Loaded capture successfully")
            return True
        except Exception as exc:
            self.log_msg(f"ERROR: {exc}")
            self.show_error("Waveform Error", exc)
            return False

    def load_waveform_and_fft(self, path):
        if self.load_waveform(path):
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
        width = max(320, canvas.winfo_width())
        height = max(240, canvas.winfo_height())
        pad = 36

        canvas.create_rectangle(0, 0, width, height, fill="white", outline="")
        canvas.create_line(pad, height - pad, width - 12, height - pad, fill="#333333")
        canvas.create_line(pad, 12, pad, height - pad, fill="#333333")

        if not self.last_fft:
            canvas.create_text(width / 2, height / 2, text="No FFT data", fill="#555555")
            return

        freq = [float(f) for f in self.last_fft["frequency"] if math.isfinite(float(f))]
        amp = [float(a) for a in self.last_fft["amplitude"] if math.isfinite(float(a))]
        n = min(len(freq), len(amp))
        freq = freq[:n]
        amp = amp[:n]
        if len(freq) < 2 or not amp or max(amp) <= 0:
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
            if math.isfinite(x) and math.isfinite(y):
                points.extend((min(max(x, pad), width - 12), min(max(y, 12), height - pad)))

        if len(points) >= 4:
            canvas.create_line(*points, fill="#0b6f85", width=2)

        canvas.create_text(pad, 8, text=f"Max {max_amp:.3g}", anchor="nw", fill="#333333")
        canvas.create_text(width - 12, height - pad + 8, text=f"{max_freq:.3g} Hz", anchor="ne", fill="#333333")


def main():
    root = tk.Tk()
    FlukeScopeSuiteProV3(root)
    root.mainloop()
