# CDC worker image (task T4).
FROM python:3.12-slim

WORKDIR /srv
COPY scripts/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY common /srv/common
COPY worker /srv/worker

CMD ["python3", "-m", "worker.cdc_processor"]
