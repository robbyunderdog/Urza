# Slim image is plenty — the bot has no compiled dependencies beyond what
# pip wheels already ship (asyncpg, aiohttp both publish manylinux wheels).
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so Docker can cache this layer independently
# of application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as a non-root user. Created before copying the app so the files can
# be owned by it — COPY otherwise keeps the build machine's ownership and
# permissions, and a checkout readable only by root (e.g. cloned as root
# with a strict umask) leaves the bot unable to read its own code.
RUN useradd --create-home --uid 1000 urza

# Now copy the rest of the application.
COPY --chown=urza:urza . .
USER urza

# Unbuffered output so `docker logs` shows log lines as they're emitted
# instead of waiting for Python's stdout buffer to flush.
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
