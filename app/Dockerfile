# Query API image (task T7).
FROM python:3.12-slim

WORKDIR /srv
COPY app/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY common /srv/common
COPY frontend /srv/frontend
COPY app /srv/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
