import os
import csv
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional, List

from Fluke289 import Fluke289


PORT = "COM5"          # change this
OUT_DIR = "fluke289_exports"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    return out.strip().strip(".") or "recording"


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    return str(value)


def to_iso(ts: Any) -> str:
    """
    Accepts:
      - time.struct_time
      - tuple/list compatible with struct_time
      - datetime
      - string
      - epoch seconds
    Returns ISO-like text.
    """
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)
    if isinstance(ts, str):
        return ts
    try:
        # time.struct_time or tuple/list
        return time.strftime("%Y-%m-%d %H:%M:%S", ts)
    except Exception:
        return to_text(ts)


def calc_duration(start_ts: Any, end_ts: Any) -> str:
    try:
        start_epoch = time.mktime(start_ts)
        end_epoch = time.mktime(end_ts)
        seconds = int(end_epoch - start_epoch)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        return f"{d:02d}:{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return ""


def get_method(obj: Any, candidates: List[str]):
    """
    Find the first callable attribute matching any candidate name.
    """
    for name in candidates:
        fn = getattr(obj, name, None)
        if callable(fn):
            return fn
    return None


class Fluke289RecordingExporter:
    def __init__(self, port: str):
        self.meter = Fluke289(port)

        # Resolve likely method names from what you showed.
        self.fn_id = get_method(self.meter, ["id", "qid", "queryID"])
        self.fn_qsls = get_method(self.meter, ["qsls", "querySavedList", "querySavedRecordingList"])
        self.fn_qrsi = get_method(self.meter, ["qrsi", "queryRecordingInfo", "querySavedRecordingInfo"])
        self.fn_qsrr = get_method(self.meter, ["qsrr", "querySavedReadingRecord", "querySavedReading"])

        if not self.fn_qsls or not self.fn_qrsi or not self.fn_qsrr:
            raise RuntimeError(
                "Could not find required saved-recording methods on Fluke289 object.\n"
                "Need equivalents of qsls(), qrsi(index), and qsrr(reading_index, sample_index)."
            )

    def meter_id(self) -> str:
        if self.fn_id:
            try:
                return to_text(self.fn_id())
            except Exception as e:
                return f"<ID query failed: {e}>"
        return "<ID method not available>"

    def list_recordings(self) -> List[Dict[str, Any]]:
        """
        Returns a normalized list of recordings.
        Expected source behavior:
          qsls() -> dict containing nb_recordings
          qrsi(index) -> dict with recording metadata
        """
        raw = self.fn_qsls()
        if not isinstance(raw, dict):
            raise RuntimeError(f"qsls() returned unexpected type: {type(raw)}")

        nb_recordings = int(raw.get("nb_recordings", 0))
        result = []

        for idx in range(nb_recordings):
            info = self.fn_qrsi(str(idx))
            if not isinstance(info, dict):
                raise RuntimeError(f"qrsi({idx}) returned unexpected type: {type(info)}")

            entry = {
                "index_1based": idx + 1,
                "index_0based": idx,
                "name": to_text(info.get("name", f"recording_{idx+1}")),
                "reading_index": info.get("reading_index"),
                "sample_interval": info.get("sample_interval"),
                "num_samples": info.get("num_samples"),
                "start_ts_raw": info.get("start_ts"),
                "end_ts_raw": info.get("end_ts"),
                "start_ts": to_iso(info.get("start_ts")),
                "end_ts": to_iso(info.get("end_ts")),
                "duration": calc_duration(info.get("start_ts"), info.get("end_ts")),
                "raw": info,
            }
            result.append(entry)

        return result

    def read_sample(self, reading_index: Any, sample_index: int) -> Dict[str, Any]:
        """
        Normalize one stored sample record.
        Expected source structure similar to what you pasted:
          {
            'start_ts': ...,
            'duration': ...,
            'record_type': ...,
            'stable': ...,
            'readings2': {'PRIMARY': {'value': ..., 'unit': ...}},
            'readings': {
                'MAXIMUM': {'value': ..., 'unit': ..., 'decimals': ...},
                'AVERAGE': {'value': ..., 'unit': ..., 'decimals': ...},
                'MINIMUM': {'value': ..., 'unit': ..., 'decimals': ...},
            }
          }
        """
        m = self.fn_qsrr(str(reading_index), str(sample_index))
        if not isinstance(m, dict):
            raise RuntimeError(f"qsrr({reading_index}, {sample_index}) returned unexpected type: {type(m)}")

        readings = m.get("readings", {}) or {}
        readings2 = m.get("readings2", {}) or {}

        primary = (readings2.get("PRIMARY") or {})
        maximum = (readings.get("MAXIMUM") or {})
        average = (readings.get("AVERAGE") or {})
        minimum = (readings.get("MINIMUM") or {})

        return {
            "sample_index": sample_index,
            "start_ts": to_iso(m.get("start_ts")),
            "duration_seconds": m.get("duration", ""),
            "record_type": m.get("record_type", ""),
            "stable": m.get("stable", ""),
            "primary_value": primary.get("value", ""),
            "primary_unit": primary.get("unit", ""),
            "maximum_value": maximum.get("value", ""),
            "maximum_unit": maximum.get("unit", ""),
            "average_value": average.get("value", ""),
            "average_unit": average.get("unit", ""),
            "minimum_value": minimum.get("value", ""),
            "minimum_unit": minimum.get("unit", ""),
            "raw": m,
        }

    def export_recording(self, rec: Dict[str, Any], out_dir: str) -> str:
        """
        Exports one recording to:
          CSV  -> samples
          JSON -> metadata/raw info
        Returns the CSV path.
        """
        ensure_dir(out_dir)

        rec_name = rec["name"] or f"recording_{rec['index_1based']}"
        stem = f"{rec['index_1based']:02d}_{safe_filename(rec_name)}"

        csv_path = os.path.join(out_dir, stem + ".csv")
        json_path = os.path.join(out_dir, stem + ".json")

        metadata = {
            "index_1based": rec["index_1based"],
            "index_0based": rec["index_0based"],
            "name": rec["name"],
            "reading_index": rec["reading_index"],
            "sample_interval": rec["sample_interval"],
            "num_samples": rec["num_samples"],
            "start_ts": rec["start_ts"],
            "end_ts": rec["end_ts"],
            "duration": rec["duration"],
            "raw_recording_info": rec["raw"],
        }

        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(metadata, jf, indent=2, default=to_text)

        with open(csv_path, "w", newline="", encoding="utf-8") as cf:
            writer = csv.writer(cf)
            writer.writerow([
                "recording_index",
                "recording_name",
                "sample_index",
                "start_ts",
                "duration_seconds",
                "record_type",
                "stable",
                "primary_value",
                "primary_unit",
                "maximum_value",
                "maximum_unit",
                "average_value",
                "average_unit",
                "minimum_value",
                "minimum_unit",
            ])

            num_samples = int(rec["num_samples"] or 0)
            for sample_index in range(num_samples):
                row = self.read_sample(rec["reading_index"], sample_index)
                writer.writerow([
                    rec["index_1based"],
                    rec["name"],
                    row["sample_index"],
                    row["start_ts"],
                    row["duration_seconds"],
                    row["record_type"],
                    row["stable"],
                    row["primary_value"],
                    row["primary_unit"],
                    row["maximum_value"],
                    row["maximum_unit"],
                    row["average_value"],
                    row["average_unit"],
                    row["minimum_value"],
                    row["minimum_unit"],
                ])

        return csv_path


def print_recording_summary(recordings: List[Dict[str, Any]]) -> None:
    print("\nSaved recordings:")
    print("-" * 100)
    print(f"{'Idx':>3}  {'Name':<28}  {'Start':<19}  {'End':<19}  {'Samples':>8}  {'Dur'}")
    print("-" * 100)
    for rec in recordings:
        print(
            f"{rec['index_1based']:>3}  "
            f"{rec['name'][:28]:<28}  "
            f"{rec['start_ts']:<19}  "
            f"{rec['end_ts']:<19}  "
            f"{str(rec['num_samples']):>8}  "
            f"{rec['duration']}"
        )
    print("-" * 100)


def main():
    ensure_dir(OUT_DIR)

    exporter = Fluke289RecordingExporter(PORT)

    print("Connected meter:", exporter.meter_id())

    recordings = exporter.list_recordings()
    if not recordings:
        print("No stored recordings found.")
        return

    print_recording_summary(recordings)

    choice = input(
        "\nEnter recording number to export, 'all' to export everything, or press Enter to quit: "
    ).strip().lower()

    if not choice:
        print("No export selected.")
        return

    if choice == "all":
        selected = recordings
    else:
        if not choice.isdigit():
            print("Invalid selection.")
            return
        rec_num = int(choice)
        selected = [r for r in recordings if r["index_1based"] == rec_num]
        if not selected:
            print("Recording not found.")
            return

    for rec in selected:
        print(f"Exporting recording {rec['index_1based']}: {rec['name']}")
        csv_path = exporter.export_recording(rec, OUT_DIR)
        print("  Wrote:", csv_path)

    print("\nDone.")


if __name__ == "__main__":
    main()