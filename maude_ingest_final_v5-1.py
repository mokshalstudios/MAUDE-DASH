import os
import csv
import duckdb
import tempfile
import glob
import time

DELIM = "|"
DB_FILE = "maude_final.duckdb"

def normalize_to_utf8_pipe(in_path):
    """
    The proven pre-processing function, now with a definitive fix
    to handle and remove the UTF-8 Byte Order Mark (BOM).
    """
    temp_fd, temp_path = tempfile.mkstemp(suffix=".csv")
    os.close(temp_fd)
    try:
        with open(in_path, "r", errors="ignore", encoding='latin-1') as fin, open(temp_path, "w", newline="", encoding="utf-8") as fout:
            writer = csv.writer(fout, delimiter=DELIM)
            
            header = fin.readline()
            
            # --- THE DEFINITIVE BOM FIX ---
            # This robustly checks for the BOM in both its direct unicode form
            # and its misinterpreted text form ('ï»¿') from the error log.
            if header.startswith(('\ufeff', 'ï»¿')):
                header = header.lstrip('\ufeffï»¿')
            # --- END FIX ---
            
            header = header.strip().replace("\t", DELIM)
            header_parts = header.split(DELIM)
            num_columns = len(header_parts)
            writer.writerow(header_parts)

            for line in fin:
                line = line.strip()
                if not line: continue
                line = line.replace("\t", DELIM)
                parts = [p.strip() for p in line.split(DELIM)]
                
                while len(parts) < num_columns: parts.append('')
                writer.writerow(parts[:num_columns])
    except Exception as e:
        print(f"   -> Critical error during pre-processing of {os.path.basename(in_path)}: {e}")
        os.remove(temp_path)
        return None
    return temp_path

def import_text_file(con, table, path):
    tmp = normalize_to_utf8_pipe(path)
    if tmp is None: return False
    try:
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv_auto('{tmp}', delim='{DELIM}', header=1, all_varchar=true, ignore_errors=true);")
        print(f"✅ Created table {table} from {os.path.basename(path)}")
        return True
    except Exception as e:
        print(f"⚠️  Could not create table from {path}: {e}")
        return False
    finally:
        if tmp and os.path.exists(tmp): os.remove(tmp)

def append_text_file(con, table, path):
    tmp = normalize_to_utf8_pipe(path)
    if tmp is None: return
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM read_csv_auto('{tmp}', delim='{DELIM}', header=1, all_varchar=true, ignore_errors=true);")
        print(f"   → Appended {os.path.basename(path)}")
    except Exception as e:
        print(f"⚠️  Skipped append for {path}: {e}")
    finally:
        if tmp and os.path.exists(tmp): os.remove(tmp)

def ingest_group(con, label, patterns, table_name):
    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    files = sorted(list(set(files)))
    
    if not files:
        print(f"ℹ️  No {label} files found matching patterns.")
        return
        
    print(f"📥 Ingesting {label} ({len(files)} files) …")
    if import_text_file(con, table_name, files[0]):
        for f in files[1:]:
            append_text_file(con, table_name, f)

    try:
        if "MDR_REPORT_KEY" in [c[0] for c in con.execute(f"DESCRIBE {table_name}").fetchall()]:
            con.execute(f"ALTER TABLE {table_name} ALTER MDR_REPORT_KEY SET DATA TYPE VARCHAR;")
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_key ON {table_name}(MDR_REPORT_KEY);")
    except Exception:
        pass
    print(f"✅ Finished {label}\n")

def main():
    start_time = time.time()
    db_path = "maude_final.duckdb"
    raw_dir = "."

    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"🗑️  Removed old database '{db_path}' to ensure a clean build.")
    
    con = duckdb.connect(db_path)
    print("🔧 MAUDE Ingestion Started\n")

    # --- PROCESSING PATIENT DATA FIRST ---
    patient_patterns = [
        os.path.join(raw_dir, "patient.txt"),
        os.path.join(raw_dir, "patient_utf8.txt"),
        os.path.join(raw_dir, "patientThru*.txt")
    ]
    ingest_group(con, "PATIENT", patient_patterns, "patient")
    # --- END OF PRIORITIZED SECTION ---

    ingest_group(con, "DEVICE", [os.path.join(raw_dir, "device*.txt")], "device")
    ingest_group(con, "FOITEXT", [os.path.join(raw_dir, "foitext*.txt")], "foi")
    ingest_group(con, "MDRFOI", [os.path.join(raw_dir, "mdrfoi*.txt")], "mdr")
    ingest_group(con, "PATIENT PROBLEM CODES", [os.path.join(raw_dir, "patientproblemcode.txt")], "patient_problem_codes")

    dev_problem_path = os.path.join(raw_dir, "foidevproblem.txt")
    if os.path.exists(dev_problem_path):
        print("📥 Ingesting headerless file (foidevproblem.txt)...")
        try:
            con.execute(f"""
                CREATE OR REPLACE TABLE foidevproblem (MDR_REPORT_KEY VARCHAR, DEVICE_PROBLEM_CODE VARCHAR);
                INSERT INTO foidevproblem SELECT * FROM read_csv('{dev_problem_path}', delim='|', all_varchar=true);
            """)
            con.execute("CREATE INDEX idx_foidevproblem_key ON foidevproblem(MDR_REPORT_KEY);")
            print("✅ Finished foidevproblem\n")
        except Exception as e:
            print(f"⚠️  Could not ingest foidevproblem.txt: {e}")

    ingest_group(con, "DEVICE PROBLEM DICT", [os.path.join(raw_dir, "deviceproblemcodes.csv")], "device_problem_dict")
    ingest_group(con, "PATIENT PROBLEM DICT", [os.path.join(raw_dir, "patientproblemcode.csv")], "patient_problem_dict")

    elapsed_min = (time.time() - start_time) / 60
    print(f"\n🎉 Ingestion complete in {elapsed_min:.1f} minutes.")
    con.close()

if __name__ == "__main__":
    main()