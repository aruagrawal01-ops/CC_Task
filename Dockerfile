FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER nobody
EXPOSE 8000
# gthread: a delayed payment sleeps in a thread without holding a DB connection.
# keep-alive 75 > nginx upstream keepalive_timeout (60s), so nginx never reuses a socket gunicorn just closed (502).
CMD ["gunicorn", "-k", "gthread", "-w", "4", "--threads", "8", "-b", "0.0.0.0:8000", "--keep-alive", "75", "--no-control-socket", "app.main:create_app()"]
