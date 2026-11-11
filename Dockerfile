# STRETCH GOAL 4
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORBIT_DB=/data/baggage.db

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY db.py claims.py main.py seed_claims.csv pytest.ini ruff.toml .gitignore ./
COPY tests/ ./tests/
COPY .claude/settings.json ./.claude/settings.json
COPY .claude/hooks/ ./.claude/hooks/

RUN mkdir -p /data \
    && python -m pytest -q \
    && python -m ruff check .

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]

