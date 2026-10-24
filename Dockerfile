# Recipe for packaging the app into a container image.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

CMD ["gunicorn", "-w", "4", "--threads", "8", "-b", "0.0.0.0:8000", "app:app"]
