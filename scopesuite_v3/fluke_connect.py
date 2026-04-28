import csv
import re
import shutil
import time
from pathlib import Path


DEFAULT_FLUKE_CONNECT_INBOX = Path("C:/FlukeConnect_Inbox")


CANONICAL_HEADERS = [
    "source_file",
    "model",
    "tool_name",
    "measurement_datetime",
    "configuration",
    "measurement",
    "unit",
    "additional_information",
    "note",
    "asset",
    "work_order",
]


HEADER_ALIASES = {
    "model number": "model",
    "model": "model",
    "tool name": "tool_name",
    "tool": "tool_name",
    "measurement date": "measurement_datetime",
    "measurement datetime": "measurement_datetime",
    "date": "measurement_datetime",
    "time": "measurement_datetime",
    "configuration": "configuration",
    "config": "configuration",
    "measurement": "measurement",
    "value": "measurement",
    "unit": "unit",
    "units": "unit",
    "additional information": "additional_information",
    "additional info": "additional_information",
    "note": "note",
    "notes": "note",
    "asset": "asset",
    "work order": "work_order",
    "workorder": "work_order",
}


def _clean_header(value):
    value = (value or "").replace("\ufeff", "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _canonical_header(value):
    cleaned = _clean_header(value).lower()
    return HEADER_ALIASES.get(cleaned, re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_"))


def parse_fluke_csv(path):
    path = Path(path)
    rows = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        raw_headers = reader.fieldnames or []
        header_map = {_clean_header(header): _canonical_header(header) for header in raw_headers}
        for raw_row in reader:
            normalized = {key: "" for key in CANONICAL_HEADERS}
            normalized["source_file"] = path.name
            for raw_key, raw_value in raw_row.items():
                key = header_map.get(_clean_header(raw_key), _canonical_header(raw_key))
                if key in normalized:
                    normalized[key] = (raw_value or "").replace("\ufeff", "").strip()
            if any(normalized.get(key) for key in CANONICAL_HEADERS if key != "source_file"):
                rows.append(normalized)
    return rows


def summarize_measurements(rows):
    by_unit = {}
    by_model = {}
    by_asset = {}
    for row in rows:
        unit = row.get("unit") or "unknown"
        model = row.get("model") or "unknown"
        asset = row.get("asset") or "unknown"
        by_unit[unit] = by_unit.get(unit, 0) + 1
        by_model[model] = by_model.get(model, 0) + 1
        by_asset[asset] = by_asset.get(asset, 0) + 1
    return {
        "rows": len(rows),
        "by_unit": by_unit,
        "by_model": by_model,
        "by_asset": by_asset,
    }


def write_normalized_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_summary_txt(path, source_dir, csv_files, pdf_files, rows, summary):
    lines = [
        "FLUKE CONNECT IMPORT SUMMARY",
        "============================",
        "",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Source folder: {source_dir}",
        f"CSV files imported: {len(csv_files)}",
        f"PDF files copied: {len(pdf_files)}",
        f"Measurement rows normalized: {summary['rows']}",
        "",
        "Models:",
    ]
    for key, count in sorted(summary["by_model"].items()):
        lines.append(f"- {key}: {count}")
    lines.append("")
    lines.append("Units:")
    for key, count in sorted(summary["by_unit"].items()):
        lines.append(f"- {key}: {count}")
    lines.append("")
    lines.append("Assets:")
    for key, count in sorted(summary["by_asset"].items()):
        lines.append(f"- {key}: {count}")
    lines.extend([
        "",
        "Files:",
        "- fluke_connect_measurements_normalized.csv",
    ])
    for pdf in pdf_files:
        lines.append(f"- {pdf.name}")
    path = Path(path)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def import_fluke_connect_folder(source_dir, output_dir):
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    if not source_dir.exists():
        raise FileNotFoundError(f"Fluke Connect inbox not found: {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(source_dir.glob("*.csv"))
    pdf_files = sorted(source_dir.glob("*.pdf"))
    if not csv_files and not pdf_files:
        raise RuntimeError(f"No Fluke Connect CSV or PDF files found in {source_dir}")

    rows = []
    for csv_file in csv_files:
        shutil.copyfile(csv_file, output_dir / csv_file.name)
        rows.extend(parse_fluke_csv(csv_file))
    for pdf_file in pdf_files:
        shutil.copyfile(pdf_file, output_dir / pdf_file.name)

    normalized_csv = write_normalized_csv(output_dir / "fluke_connect_measurements_normalized.csv", rows)
    summary = summarize_measurements(rows)
    summary_txt = write_summary_txt(
        output_dir / "FLUKE_CONNECT_IMPORT_SUMMARY.txt",
        source_dir,
        csv_files,
        pdf_files,
        rows,
        summary,
    )
    return {
        "source_dir": source_dir,
        "output_dir": output_dir,
        "csv_files": csv_files,
        "pdf_files": pdf_files,
        "rows": rows,
        "summary": summary,
        "normalized_csv": normalized_csv,
        "summary_txt": summary_txt,
    }
