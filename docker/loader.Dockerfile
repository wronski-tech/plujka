FROM python:3.11-slim

WORKDIR /app

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY loader/requirements.txt /app/loader-requirements.txt
RUN pip install --no-cache-dir -r /app/loader-requirements.txt

COPY api /app/api
COPY scripts /app/scripts
COPY data /app/data
COPY streamlit_app/geo/gminy_pre1999.json /app/streamlit_app/geo/gminy_pre1999.json

# Override command from docker compose run if needed.
CMD ["python", "-u", "scripts/kbw_stage_duckdb.py", "--help"]
