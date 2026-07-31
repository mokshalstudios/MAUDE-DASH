# MaudeDash — Research Edition container
# ======================================
#
# For hosting the Streamlit app on a platform that allows long-running
# processes (Fly.io, Hugging Face Spaces, Render, Railway). This is NOT for
# DreamHost shared hosting — see deploy/DREAMHOST.md for why, and use the
# static web tier there instead.
#
# The image expects a slimmed database rather than the full ~73 GB build.
# Produce one with:
#
#   python packaging/maude_export_web.py --db maude_final.duckdb --out ./slim
#   python - <<'PY'
#   import duckdb, glob
#   con = duckdb.connect("maude_web.duckdb")
#   files = sorted(glob.glob("slim/mdr_*.parquet"))
#   con.execute(f"CREATE TABLE mdr_flat AS SELECT * FROM read_parquet({files!r})")
#   for t, f in (("flat_dev_problems","slim/devprob.parquet"),
#                ("flat_pat_problems","slim/patprob.parquet"),
#                ("device_problem_dict","slim/dict_device.parquet"),
#                ("patient_problem_dict","slim/dict_patient.parquet")):
#       con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{f}')")
#   for c in ("MDR_REPORT_KEY","report_year","product_code","manufacturer"):
#       con.execute(f"CREATE INDEX idx_mf_{c} ON mdr_flat({c})")
#   con.close()
#   PY
#
# Build & run:
#   docker build -f deploy/Dockerfile -t maudedash .
#   docker run -p 8501:8501 -v $PWD/maude_web.duckdb:/data/maude.duckdb maudedash

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAUDEDASH_DB=/data/maude.duckdb \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Build deps for scipy/wordcloud wheels, removed in the same layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y gcc g++ && apt-get autoremove -y

COPY research-edition/ /app/

# Hugging Face Spaces and Fly both route to $PORT; default to Streamlit's.
ENV PORT=8501
EXPOSE 8501

# Run as non-root; the database mount is read-only to the app.
RUN useradd --create-home --uid 1000 maude && chown -R maude:maude /app
USER maude

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
      sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/_stcore/health', timeout=4).status==200 else 1)"

CMD streamlit run maudedash_app.py \
      --server.port=${PORT} \
      --server.address=0.0.0.0 \
      --server.enableCORS=false \
      --server.enableXsrfProtection=true \
      -- --db ${MAUDEDASH_DB}
