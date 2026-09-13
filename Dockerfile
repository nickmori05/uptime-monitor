FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --uid 10001 --create-home monitor && mkdir /data && chown monitor:monitor /data
COPY monitor.py incidents.py ./
USER monitor
ENTRYPOINT ["python", "monitor.py", "--database", "/data/checks.sqlite3"]
CMD ["--help"]
