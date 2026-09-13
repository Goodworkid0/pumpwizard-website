FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 pump \
    && useradd --uid 10001 --gid pump --home-dir /app --no-create-home --shell /usr/sbin/nologin pump

WORKDIR /app

COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY --chown=pump:pump paper_trader.py ./
COPY --chown=pump:pump *.json ./
COPY --chown=pump:pump paper/ ./paper/
COPY --chown=pump:pump web/ ./web/
COPY --chown=pump:pump scripts/ ./scripts/
COPY --chown=pump:pump exports/ladder-research-20260911/analysis.json ./exports/ladder-research-20260911/analysis.json

USER pump

EXPOSE 8765

CMD ["python3", "paper_trader.py", "live", "--config", "/app/paper.cadence.config.json", "--db", "/app/data/cadence.sqlite", "--port", "8765", "--bind", "0.0.0.0"]
