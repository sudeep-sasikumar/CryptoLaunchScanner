# Launch Radar - Linux image for a VPS (see README "Running on the VPS" and docker-compose.yaml).
# The dashboard login is enforced automatically: docker-compose.yaml binds the dashboard to 0.0.0.0,
# and the app refuses to serve beyond localhost without DASHBOARD_PASSWORD.
FROM python:3.12-slim

# the git commit this image was built from (set by the GitHub workflow); printed at start
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 1000 radar && mkdir -p /app/var && chown -R radar /app
USER radar

EXPOSE 8010
CMD ["python", "main.py"]
