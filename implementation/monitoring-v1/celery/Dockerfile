FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /monitoring

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY celery_app.py tasks.py monitoring_store.py ./
COPY reporting/ ./reporting/

# Ensure the non-root runtime user can read application modules.
RUN chmod 0644 celery_app.py tasks.py monitoring_store.py reporting/*.py

RUN useradd --create-home --uid 10001 celeryuser
USER 10001:10001

CMD ["celery", "-A", "celery_app.app", "worker", "--loglevel=INFO", "--concurrency=4"]
