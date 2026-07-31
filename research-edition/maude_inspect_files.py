"""
MAUDE file inspector
====================

Walks a folder of MAUDE files and identifies what each file actually IS based
on its content, not its name. Useful because the FDA's filenames are
notoriously confusing (the FDA's own documentation has the FOIDEVPROBLEM /
DEVICEPROBLEMCODES labels inverted compared to the actual file contents).

Usage:
    python maude_inspect_files.py --raw-dir "C:\\path\\to\\maude_files"

Output: per-file identification, plus a summary at the end of which FDA-
distributed tables are present and which (if any) are missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def sniff(path: Path) -> dict:
    """Read first ~2KB of a file and report what we see."""
    info: dict = {"path": str(path), "size": path.stat().st_size}
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
        # Decode with fallbacks
        try:
            text = head.decode("utf-8-sig")
            info["encoding"] = "utf-8"
        except UnicodeDecodeError:
            text = head.decode("latin-1", errors="replace")
            info["encoding"] = "latin-1"
        # Strip BOM if it survived decoding
        text = text.lstrip("\ufeff").lstrip("\ufeff")
        lines = [ln for ln in text.split("\n")[:5] if ln.strip()]
        info["first_line"] = (lines[0][:120] if lines else "")
        # Detect delimiter
        line0 = lines[0] if lines else ""
        pipe_count = line0.count("|")
        comma_count = line0.count(",")
        info["delimiter"] = "|" if pipe_count >= comma_count else ","
        # Parse 3 rows
        rows = [ln.split(info["delimiter"]) for ln in lines[:3]]
        info["columns"] = len(rows[0]) if rows else 0
        info["sample_rows"] = [[c.strip()[:40] for c in r] for r in rows]
    except Exception as e:
        info["error"] = str(e)
    return info


def classify(info: dict) -> tuple[str, str]:
    """Return (role, human-readable description) based on content."""
    if "error" in info:
        return "ERROR", info["error"]
    name = os.path.basename(info["path"]).lower()
    ncols = info.get("columns", 0)
    rows = info.get("sample_rows", [])
    first_field = (rows[0][0].strip() if rows and rows[0] else "")
    second_field = (rows[0][1].strip() if rows and len(rows[0]) > 1 else "")

    # --- Primary MAUDE tables (no ambiguity) ---
    if "mdrfoi" in name:
        return ("mdr_master",
                "Master event records (one row per MDR submission)")
    if name.startswith("device") and "problem" not in name:
        return ("device_records",
                "Device records (BRAND_NAME, GENERIC_NAME, product code, etc.)")
    if "foitext" in name:
        return ("foi_narratives", "Narrative text records (event descriptions)")
    if name.startswith("patient") and "problem" not in name:
        return ("patient_records", "Patient records (age, sex, outcomes)")

    # --- Problem code files (ambiguous by name — use content) ---
    if "problem" in name or "code" in name:
        is_csv = info["delimiter"] == ","
        # First check: is this an enriched 4-column dictionary CSV?
        if is_csv and ncols >= 3:
            if rows and any(c.upper() == "FDA_CODE" for c in rows[0]):
                kind = ("patient_problem_dict" if "patient" in name
                        else "device_problem_dict")
                return (kind,
                        f"Enriched dictionary CSV (FDA_CODE, TERM, NCIT_CODE, "
                        f"IMDRF_CODE); {info['size']:,} bytes")
        # Check: 5+ column headered format (current FDA)
        # Header looks like a header if first row has alphabetic column names
        if ncols >= 4 and rows:
            header_candidates = [c.strip().upper() for c in rows[0]]
            if "MDR_REPORT_KEY" in header_candidates and any(
                "PROBLEM_CODE" in c or "PROBLEM" == c for c in header_candidates
            ):
                kind = ("patient_problem_data" if "patient" in name
                        else "device_problem_data")
                return (kind,
                        f"Per-report data, {ncols}-column WITH HEADER "
                        f"(current FDA format). Header: {rows[0]}")
        # The most reliable signal: dictionary files have descriptive TEXT in
        # the second field (e.g., "Material Fracture"); per-report data files
        # have a short numeric code there. Use that as primary discriminator,
        # falling back to file-size heuristics when fields are ambiguous.
        second_is_text = bool(second_field) and not second_field.isdigit() and any(c.isalpha() for c in second_field)
        second_is_numeric = bool(second_field) and second_field.isdigit()
        size_mb = info["size"] / (1024 * 1024)
        # Real per-report data files are >10 MB; dictionaries are <100 KB
        size_suggests_data = size_mb > 1.0
        size_suggests_dict = size_mb < 0.5

        if ncols == 2:
            if second_is_text:
                # Definitely a dictionary
                kind = ("patient_problem_dict" if "patient" in name
                        else "device_problem_dict")
                return (kind,
                        f"Dictionary (code={first_field}, term=\"{second_field[:50]}\")")
            if second_is_numeric:
                # Both fields are numeric — disambiguate by size
                if size_suggests_data:
                    kind = ("patient_problem_data" if "patient" in name
                            else "device_problem_data")
                    return (kind,
                            f"Per-report data ({size_mb:.1f} MB suggests data); "
                            f"first row: MDR={first_field}, code={second_field}")
                if size_suggests_dict:
                    kind = ("patient_problem_dict" if "patient" in name
                            else "device_problem_dict")
                    return (kind,
                            f"Dictionary ({size_mb:.1f} MB suggests dict); "
                            f"first row: code={first_field}, term={second_field}")
                # Borderline — go with first-field length
                if len(first_field) >= 6:
                    kind = ("patient_problem_data" if "patient" in name
                            else "device_problem_data")
                    return (kind, f"Per-report data (MDR_REPORT_KEY={first_field}, "
                                  f"code={second_field})")
                kind = ("patient_problem_dict" if "patient" in name
                        else "device_problem_dict")
                return (kind, f"Dictionary (code={first_field}, term={second_field})")
        # Three-column patient problem code (MDR | seq | code)
        if ncols == 3:
            kind = ("patient_problem_data" if "patient" in name
                    else "device_problem_data")
            return (kind,
                    f"Per-report data, 3-column variant "
                    f"(MDR_REPORT_KEY={first_field}, seq={rows[0][1]}, "
                    f"code={rows[0][2]})")
        return ("unknown_problem_file",
                f"Problem-code file with ambiguous content: "
                f"ncols={ncols}, first_field={first_field!r}")

    return ("unknown",
            f"Unrecognised filename pattern: {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect MAUDE raw files")
    ap.add_argument("--raw-dir", default=".",
                    help="Directory containing the MAUDE .txt / .csv files")
    args = ap.parse_args()

    raw = Path(args.raw_dir).resolve()
    if not raw.is_dir():
        print(f"ERROR: {raw} is not a directory")
        return 2

    print(f"Inspecting: {raw}")
    print("=" * 100)

    files = sorted(list(raw.glob("*.txt")) + list(raw.glob("*.csv")))
    if not files:
        print("No .txt or .csv files found.")
        return 1

    findings: dict[str, list[tuple[Path, dict, str]]] = {}
    for f in files:
        info = sniff(f)
        role, detail = classify(info)
        findings.setdefault(role, []).append((f, info, detail))
        size_mb = info["size"] / (1024 * 1024)
        cols = info.get("columns", "?")
        delim = info.get("delimiter", "?")
        print(f"  [{role:25s}] {f.name:42s} {size_mb:>8.2f} MB  cols={cols} delim={delim!r}")
        print(f"    -> {detail}")

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)

    required_roles = {
        "mdr_master":            "Master event records (mdrfoi*.txt or mdrfoithru*.txt)",
        "device_records":        "Device records (device*.txt or foidev*.txt)",
        "patient_records":       "Patient records (patient*.txt or patientthru*.txt)",
        "foi_narratives":        "Narrative text (foitext*.txt)",
        "device_problem_data":   "Per-report device problems (foidevproblem.txt)",
        "device_problem_dict":   "Device problem dictionary (deviceproblemcodes.csv/txt)",
        "patient_problem_data":  "Per-report patient problems (patientproblemcode.txt)",
        "patient_problem_dict":  "Patient problem dictionary (patientproblemdata.csv/txt)",
    }

    missing: list[tuple[str, str]] = []
    for role, desc in required_roles.items():
        if role in findings:
            n = len(findings[role])
            total_mb = sum(p.stat().st_size for p, _, _ in findings[role]) / (1024 * 1024)
            print(f"  [PRESENT]  {role:25s}  {n} file(s), {total_mb:.1f} MB total")
        else:
            print(f"  [MISSING]  {role:25s}  - {desc}")
            missing.append((role, desc))

    if "unknown" in findings or "unknown_problem_file" in findings or "ERROR" in findings:
        print()
        print("Files that couldn't be classified:")
        for role in ["unknown", "unknown_problem_file", "ERROR"]:
            for p, _, detail in findings.get(role, []):
                print(f"  - {p.name}: {detail}")

    if missing:
        print()
        print("DOWNLOAD LINKS FOR MISSING FILES (FDA MAUDE):")
        for role, _ in missing:
            url = {
                "mdr_master":            "https://www.accessdata.fda.gov/MAUDE/ftparea/mdrfoithru2025.zip",
                "device_records":        "https://www.accessdata.fda.gov/MAUDE/ftparea/device2025.zip (and earlier years)",
                "patient_records":       "https://www.accessdata.fda.gov/MAUDE/ftparea/patientthru2025.zip",
                "foi_narratives":        "https://www.accessdata.fda.gov/MAUDE/ftparea/foitext2025.zip (and earlier years)",
                "device_problem_data":   "https://www.accessdata.fda.gov/MAUDE/ftparea/foidevproblem.zip",
                "device_problem_dict":   "https://www.accessdata.fda.gov/MAUDE/ftparea/deviceproblemcodes.zip",
                "patient_problem_data":  "https://www.accessdata.fda.gov/MAUDE/ftparea/patientproblemcode.zip",
                "patient_problem_dict":  "https://www.accessdata.fda.gov/MAUDE/ftparea/patientproblemdata.zip",
            }.get(role, "(see FDA MDR data files page)")
            print(f"  {role}:")
            print(f"    {url}")
        print()
        print("After downloading, unzip into the raw folder and re-run the ingest")
        print("(or use the targeted loaders for individual missing tables).")
        return 1

    print()
    print("All required MAUDE file roles present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
