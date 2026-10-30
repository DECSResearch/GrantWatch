# GrantWatch image. Built on the lab host and imported into k3s containerd (no
# registry), the same way as c2sr-testbed-llm-orchestrator; see k8s/README.md.
FROM python:3.12-slim

# PYTHONPATH: the dashboard's uvicorn --app-dir src only adds src/, and the app
# imports the top-level packages (grants, notifications, doc_checker) too.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY grants/ ./grants/
COPY grants_data/ ./grants_data/
COPY llm_utils/ ./llm_utils/
COPY logs/ ./logs/
COPY notifications/ ./notifications/
COPY doc_checker/ ./doc_checker/
COPY src/ ./src/
COPY config/ ./config/

# The daily CronJob runs this: ingest, score, then email. The dashboard
# Deployment overrides it with uvicorn.
CMD ["python", "main.py"]
