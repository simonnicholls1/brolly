FROM python:3.15-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY brolly ./brolly
COPY public ./public
# Run as an unprivileged user. Votes live in Postgres (DATABASE_URL); the SQLite
# fallback path is only used when that isn't set.
RUN useradd --system --uid 10001 --home-dir /app brolly && mkdir -p /app/data && chown brolly /app/data
USER brolly
ENV DATABASE_PATH=/app/data/brolly.db
EXPOSE 8000
# No --proxy-headers: the app reads the real client IP from Fly-Client-IP itself,
# and trusting X-Forwarded-For from anyone would let clients spoof it.
CMD ["uvicorn", "--factory", "brolly.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
