# Slim image is plenty — the bot has no compiled dependencies beyond what
# pip wheels already ship (asyncpg, aiohttp both publish manylinux wheels).
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so Docker can cache this layer independently
# of application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the application.
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 1000 urza
USER urza

# Unbuffered output so `docker logs` shows log lines as they're emitted
# instead of waiting for Python's stdout buffer to flush.
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
