import serial
import time
import threading
import subprocess
from pathlib import Path
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from serial.tools import list_ports


BAUD = 1200
DEFAULT_OUTPUT_FOLDER = Path.home() / "Desktop" / "Fluke199C_Captures"
PORT_PREFIXES = ("/dev/cu.", "/dev/tty.")


class Fluke199CDriver:
    def __init__(self, port: str, timeout: float = 2.0):
        self.port = port
        self.timeout = timeout

    def open_serial(self):
        if not self.port:
            raise RuntimeError("No serial port selected")

        return serial.Serial(
            self.port,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def read_until_cr(self, ser):
        data = bytearray()
        while True:
            b = ser.read(1)
            if not b:
                raise TimeoutError("Timeout waiting for CR")
            if b == b"\r":
                return bytes(data)
            data.extend(b)

    def send_cmd(self, ser, cmd: str):
        ser.reset_input_buffer()
        ser.write(cmd.encode("ascii") + b"\r")
        ser.flush()

        ack = self.read_until_cr(ser)
        if ack != b"0":
            raise RuntimeError(f"Fluke rejected command {cmd}: ACK={ack!r}")
        return ack

    def read_qp_payload(self, ser, progress_callback=None):
        digits = bytearray()

        while True:
            b = ser.read(1)
            if not b:
                raise TimeoutError("Timeout reading QP header")
            if b == b",":
                break
            if 48 <= b[0] <= 57:
                digits.extend(b)

        if not digits:
            raise RuntimeError("No QP byte count received")

        total = int(digits.decode("ascii"))
        payload = bytearray()

        while len(payload) < total:
            chunk = ser.read(min(4096, total - len(payload)))
            if not chunk:
                raise TimeoutError("Timeout during QP transfer")
            payload.extend(chunk)
            if progress_callback:
                progress_callback(len(payload), total)

        return bytes(payload)


class Fluke199CApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Fluke 199C Pro Capture")
        self.root.geometry("760x620")

        self.outdir = DEFAULT_OUTPUT_FOLDER
        self.outdir.mkdir(parents=True, exist_ok=True)

        self.last_png = None
        self.image_ref = None
        self.instrument_var = tk.StringVar(value="Unknown")
        self.outdir_var = tk.StringVar(value=str(self.outdir))
        self.last_var = tk.StringVar(value="None")
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.build_ui()
        self.refresh_ports()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Serial Port:").pack(side="left")

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=38)
        self.port_combo.pack(side="left", padx=6)

        ttk.Button(top, text="Refresh Ports", command=self.refresh_ports).pack(side="left", padx=4)
        ttk.Button(top, text="Choose Folder", command=self.choose_folder).pack(side="left", padx=4)

        info_frame = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        info_frame.pack(fill="x")

        ttk.Label(info_frame, text="Instrument:").grid(row=0, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.instrument_var, width=45).grid(row=0, column=1, sticky="w")
        ttk.Label(info_frame, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.outdir_var, width=45).grid(row=1, column=1, sticky="w")
        ttk.Label(info_frame, text="Last capture:").grid(row=2, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.last_var, width=45).grid(row=2, column=1, sticky="w")

        btns = ttk.Frame(self.root, padding=10)
        btns.pack(fill="x")

        ttk.Button(btns, text="Capture Screen", command=self.capture_thread).pack(side="left", padx=5)
        ttk.Button(btns, text="Test ID", command=self.test_id_thread).pack(side="left", padx=5)
        ttk.Button(btns, text="Open Last PNG", command=self.open_last).pack(side="left", padx=5)
        ttk.Button(btns, text="Open Folder", command=self.open_folder).pack(side="left", padx=5)

        self.progress = ttk.Progressbar(self.root, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(self.root, textvariable=self.status_var, padding=8).pack(fill="x")

        self.canvas = tk.Label(self.root, bg="black", relief="sunken")
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both")

        self.log = tk.Text(log_frame, height=8, wrap="none")
        self.log.pack(fill="both", expand=True)

    def log_msg(self, msg):
        self.root.after(0, lambda: self._log_msg(msg))

    def _log_msg(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{timestamp}] {msg}\n")
        self.log.see("end")
        self.status_var.set(msg)

    def refresh_ports(self):
        ports = [
            p.device for p in list_ports.comports()
            if p.device.startswith(PORT_PREFIXES)
        ]

        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

        self.log_msg("Ports refreshed")

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.outdir))
        if folder:
            self.outdir = Path(folder)
            self.outdir_var.set(str(self.outdir))
            self.log_msg(f"Output folder: {self.outdir}")

    def open_serial(self):
        port = self.port_var.get().strip()
        return Fluke199CDriver(port, timeout=5)

    def update_progress(self, current: int, total: int):
        percent = min(100.0, max(0.0, current / total * 100.0))
        self.root.after(0, lambda: self.progress_var.set(percent))

    def decode_to_png(self, raw_bytes, outfile):
        bands = []
        i = 0

        while i < len(raw_bytes) - 5:
            if raw_bytes[i] == 27 and raw_bytes[i + 1] == ord("*"):
                mode = raw_bytes[i + 2]
                n1 = raw_bytes[i + 3]
                n2 = raw_bytes[i + 4]
                columns = n1 + 256 * n2

                if mode in (0, 1, 4):
                    bytes_per_column = 1
                    band_height = 8
                elif mode in (32, 33):
                    bytes_per_column = 3
                    band_height = 24
                else:
                    i += 1
                    continue

                start = i + 5
                end = start + columns * bytes_per_column
                if end > len(raw_bytes):
                    break

                block = raw_bytes[start:end]
                band = Image.new("1", (columns, band_height), 1)

                for x in range(columns):
                    for byte_index in range(bytes_per_column):
                        b = block[x * bytes_per_column + byte_index]
                        for bit in range(8):
                            if b & (1 << (7 - bit)):
                                y = byte_index * 8 + bit
                                band.putpixel((x, y), 0)

                bands.append(band)
                i = end
            else:
                i += 1

        if not bands:
            raise RuntimeError("No ESC/P image bands found")

        width = max(b.width for b in bands)
        height = sum(b.height for b in bands)
        final = Image.new("1", (width, height), 1)

        y = 0
        for band in bands:
            final.paste(band, (0, y))
            y += band.height

        rgb_image = final.convert("RGB")
        rgb_image.save(outfile, format="PNG")
        return outfile

    def capture_thread(self):
        threading.Thread(target=self.capture_screen, daemon=True).start()

    def capture_screen(self):
        try:
            self.log_msg("Connecting to Fluke 199C...")
            self.progress_var.set(0.0)

            ts = time.strftime("%Y%m%d_%H%M%S")
            raw_file = self.outdir / f"fluke199c_{ts}.bin"
            png_file = self.outdir / f"fluke199c_{ts}.png"

            driver = self.open_serial()
            with driver.open_serial() as ser:
                self.log_msg("Sending GR command")
                driver.send_cmd(ser, "GR")

                self.log_msg("Querying instrument ID")
                driver.send_cmd(ser, "ID")
                ident = driver.read_until_cr(ser)
                instrument = ident.decode(errors="replace")
                self.instrument_var.set(instrument)
                self.log_msg(f"Instrument: {instrument}")

                self.log_msg("Requesting screen capture (QP)")
                driver.send_cmd(ser, "QP")
                raw = driver.read_qp_payload(ser, progress_callback=self.update_progress)

            raw_file.write_bytes(raw)
            self.log_msg(f"Raw saved: {raw_file}")

            self.decode_to_png(raw, png_file)
            self.last_png = png_file
            self.last_var.set(str(png_file.name))
            self.log_msg(f"PNG saved: {png_file}")

            self.show_image(png_file)
            self.progress_var.set(100.0)
        except Exception as e:
            self.log_msg(f"ERROR: {e}")
            messagebox.showerror("Fluke Capture Error", str(e))
            self.progress_var.set(0.0)

    def test_id_thread(self):
        threading.Thread(target=self.test_id, daemon=True).start()

    def test_id(self):
        try:
            driver = self.open_serial()
            with driver.open_serial() as ser:
                self.log_msg("Sending GR command")
                driver.send_cmd(ser, "GR")
                self.log_msg("Querying instrument ID")
                driver.send_cmd(ser, "ID")
                ident = driver.read_until_cr(ser)
                instrument = ident.decode(errors="replace")
                self.instrument_var.set(instrument)
                self.log_msg(f"Instrument: {instrument}")
        except Exception as e:
            self.log_msg(f"ERROR: {e}")
            messagebox.showerror("Fluke ID Error", str(e))

    def show_image(self, path):
        img = Image.open(path).convert("RGB")
        self.root.update_idletasks()
        max_width = max(200, self.canvas.winfo_width())
        max_height = max(200, self.canvas.winfo_height())
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

        self.image_ref = ImageTk.PhotoImage(img)
        self.root.after(0, lambda: self.canvas.config(image=self.image_ref))

    def open_last(self):
        if self.last_png and self.last_png.exists():
            subprocess.run(["open", str(self.last_png)])
        else:
            messagebox.showinfo("No Image", "No PNG captured yet")

    def open_folder(self):
        subprocess.run(["open", str(self.outdir)])


if __name__ == "__main__":
    root = tk.Tk()
    app = Fluke199CApp(root)
    root.mainloop()