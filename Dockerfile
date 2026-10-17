FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER nobody
EXPOSE 8000
# gthread: a delayed payment sleeps in a thread without holding a DB connection
CMD ["gunicorn", "-k", "gthread", "-w", "4", "--threads", "8", "-b", "0.0.0.0:8000", "app.main:create_app()"]
