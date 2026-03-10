FROM python:3.12-slim

WORKDIR /app

# Create non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Switch to non-root user
USER appuser

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8000}"]
