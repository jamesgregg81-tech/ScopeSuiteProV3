import contextlib
import ctypes
from ctypes import wintypes
import os
import queue
import threading
import traceback

import Fluke_Replay_Final_Tool_A_V_B_I as analyzer


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
LRESULT = ctypes.c_ssize_t

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SetWindowTextW.restype = wintypes.BOOL
user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.EnableWindow.restype = wintypes.BOOL
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_APP_LOG = 0x8001
WM_SETFONT = 0x0030

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
WS_BORDER = 0x00800000
ES_LEFT = 0x0000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
BS_PUSHBUTTON = 0x00000000

SW_SHOW = 5
CW_USEDEFAULT = 0x80000000

IDC_PORT = 100
IDC_STATUS = 101
IDC_LOG = 102
IDC_CONNECT = 201
IDC_AUTO = 202
IDC_CAPTURE = 203
IDC_REPORT = 204
IDC_FOLDER = 205
IDC_EXIT = 206


WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterClassW.restype = wintypes.ATOM


class QueueWriter:
    def __init__(self, app):
        self.app = app

    def write(self, text):
        if text:
            self.app.queue_log(text)

    def flush(self):
        pass


class NativeGui:
    def __init__(self):
        self.hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self.class_name = "FlukeScopeMeterAnalyzerGui"
        self.controls = {}
        self.log_queue = queue.Queue()
        self.ser = None
        self.ident = ""
        self.last_export = None
        self.worker = None
        self.wndproc = WNDPROC(self._wnd_proc)
        self.font = gdi32.GetStockObject(17)

    def run(self):
        self._register_class()
        self.hwnd = user32.CreateWindowExW(
            0,
            self.class_name,
            "Fluke ScopeMeter Analyzer",
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            940,
            640,
            None,
            None,
            self.hinst,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError()

        self._create_controls()
        self.log("Fluke ScopeMeter Analyzer GUI ready.\r\n")
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _register_class(self):
        wc = WNDCLASS()
        wc.lpfnWndProc = self.wndproc
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.hbrBackground = 5
        wc.lpszClassName = self.class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()

    def _create_controls(self):
        self._label("Fluke ScopeMeter Analyzer", 18, 14, 300, 24)
        self._label("COM Port", 18, 52, 70, 22)
        self.controls[IDC_PORT] = self._edit(analyzer.PORT, 92, 50, 90, 24, IDC_PORT)
        self._label("Scope", 205, 52, 45, 22)
        self.controls[IDC_STATUS] = self._label("Not connected", 255, 52, 640, 22, IDC_STATUS)

        buttons = [
            ("Connect ScopeMeter", IDC_CONNECT),
            ("Auto Detect COM", IDC_AUTO),
            ("Capture Replay", IDC_CAPTURE),
            ("Power Quality Report", IDC_REPORT),
            ("Open Reports Folder", IDC_FOLDER),
            ("Exit", IDC_EXIT),
        ]
        y = 92
        for text, control_id in buttons:
            self.controls[control_id] = self._button(text, 18, y, 185, 34, control_id)
            y += 44

        self.controls[IDC_LOG] = user32.CreateWindowExW(
            0,
            "EDIT",
            "",
            WS_CHILD | WS_VISIBLE | WS_BORDER | WS_VSCROLL | ES_LEFT | ES_MULTILINE | ES_AUTOVSCROLL | ES_READONLY,
            220,
            92,
            680,
            470,
            self.hwnd,
            IDC_LOG,
            self.hinst,
            None,
        )
        self._set_font(self.controls[IDC_LOG])

    def _label(self, text, x, y, w, h, control_id=0):
        hwnd = user32.CreateWindowExW(0, "STATIC", text, WS_CHILD | WS_VISIBLE, x, y, w, h, self.hwnd, control_id, self.hinst, None)
        self._set_font(hwnd)
        return hwnd

    def _edit(self, text, x, y, w, h, control_id):
        hwnd = user32.CreateWindowExW(0, "EDIT", text, WS_CHILD | WS_VISIBLE | WS_BORDER | ES_LEFT, x, y, w, h, self.hwnd, control_id, self.hinst, None)
        self._set_font(hwnd)
        return hwnd

    def _button(self, text, x, y, w, h, control_id):
        hwnd = user32.CreateWindowExW(0, "BUTTON", text, WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, x, y, w, h, self.hwnd, control_id, self.hinst, None)
        self._set_font(hwnd)
        return hwnd

    def _set_font(self, hwnd):
        user32.SendMessageW(hwnd, WM_SETFONT, self.font, True)

    def _get_text(self, control_id):
        hwnd = self.controls[control_id]
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        return buff.value

    def _set_text(self, control_id, text):
        user32.SetWindowTextW(self.controls[control_id], text)

    def _enable_buttons(self, enabled):
        for control_id in [IDC_CONNECT, IDC_AUTO, IDC_CAPTURE, IDC_REPORT, IDC_FOLDER]:
            user32.EnableWindow(self.controls[control_id], bool(enabled))

    def queue_log(self, text):
        self.log_queue.put(text)
        user32.PostMessageW(self.hwnd, WM_APP_LOG, 0, 0)

    def log(self, text):
        text = str(text)
        hwnd = self.controls.get(IDC_LOG)
        if not hwnd:
            return

        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)

        updated = buff.value + text
        if len(updated) > 120000:
            updated = updated[-100000:]
        user32.SetWindowTextW(hwnd, updated)

    def _drain_log_queue(self):
        while True:
            try:
                self.log(self.log_queue.get_nowait())
            except queue.Empty:
                break

    def _run_worker(self, label, func):
        if self.worker and self.worker.is_alive():
            self._message("Busy", "Another operation is already running.")
            return

        def runner():
            self.queue_log(f"\r\n{label}\r\n")
            try:
                with contextlib.redirect_stdout(QueueWriter(self)):
                    result = func()
                if result:
                    self.queue_log(str(result) + "\r\n")
                self.queue_log("Operation complete.\r\n")
            except Exception:
                self.queue_log(traceback.format_exc() + "\r\n")
                self._message("ScopeMeter Analyzer", "Operation failed. See the log for details.")
            finally:
                user32.PostMessageW(self.hwnd, WM_APP_LOG, 1, 0)

        self._enable_buttons(False)
        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def _message(self, title, text):
        user32.MessageBoxW(self.hwnd, text, title, 0)

    def _close_serial(self):
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def connect_scope(self):
        def task():
            self._close_serial()
            port = self._get_text(IDC_PORT).strip() or analyzer.PORT
            ser, ident = analyzer.connect_scope(port)
            self.ser = ser
            self.ident = ident
            self._set_text(IDC_STATUS, ident)
            return f"Connected on {ser.port}: {ident}"

        self._run_worker("Connecting ScopeMeter...", task)

    def auto_detect_com(self):
        def task():
            preferred = self._get_text(IDC_PORT).strip() or analyzer.PORT
            port, baud, _attempts = analyzer.autodetect_scope_port_and_baud(preferred)
            self._set_text(IDC_PORT, port)
            return f"Detected ScopeMeter on {port} at {baud} baud."

        self._run_worker("Auto detecting COM port...", task)

    def capture_replay(self):
        def task():
            if self.ser is None or not self.ser.is_open:
                port = self._get_text(IDC_PORT).strip() or analyzer.PORT
                self.ser, self.ident = analyzer.connect_scope(port)
                self._set_text(IDC_STATUS, self.ident)

            result = analyzer.export_replay_reports(self.ser, self.ident)
            self.last_export = result
            return f"Replay captured. Report folder: {result['export_dir']}"

        self._run_worker("Capturing replay and building reports...", task)

    def _latest_global_report(self):
        if self.last_export and os.path.exists(self.last_export.get("global_summary_txt", "")):
            return self.last_export["global_summary_txt"]

        root = analyzer.SAVE_ROOT
        if not os.path.isdir(root):
            return None

        reports = []
        for name in os.listdir(root):
            path = os.path.join(root, name, "FINAL_GLOBAL_REPORT.txt")
            if os.path.isfile(path):
                reports.append(path)
        return max(reports, key=os.path.getmtime) if reports else None

    def open_power_quality_report(self):
        report = self._latest_global_report()
        if not report:
            self._message("Power Quality Report", "No report found yet. Run Capture Replay first.")
            return
        os.startfile(report)
        self.log(f"Opened power quality report: {report}\r\n")

    def open_reports_folder(self):
        os.makedirs(analyzer.SAVE_ROOT, exist_ok=True)
        os.startfile(analyzer.SAVE_ROOT)
        self.log(f"Opened reports folder: {analyzer.SAVE_ROOT}\r\n")

    def exit_app(self):
        self._close_serial()
        user32.DestroyWindow(self.hwnd)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_COMMAND:
            control_id = int(wparam) & 0xFFFF
            if control_id == IDC_CONNECT:
                self.connect_scope()
            elif control_id == IDC_AUTO:
                self.auto_detect_com()
            elif control_id == IDC_CAPTURE:
                self.capture_replay()
            elif control_id == IDC_REPORT:
                self.open_power_quality_report()
            elif control_id == IDC_FOLDER:
                self.open_reports_folder()
            elif control_id == IDC_EXIT:
                self.exit_app()
            return 0

        if msg == WM_APP_LOG:
            self._drain_log_queue()
            if int(wparam) == 1:
                self._enable_buttons(True)
            return 0

        if msg == WM_CLOSE:
            self.exit_app()
            return 0

        if msg == WM_DESTROY:
            self._close_serial()
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main():
    NativeGui().run()


if __name__ == "__main__":
    main()
