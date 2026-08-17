# Playwright's own image. It ships Chromium AND the ~80 system libraries the
# browser needs, which is the entire reason to use it -- installing those onto
# a plain python image by hand is a bad afternoon.
#
# IMPORTANT: the image tag and the pip package version below must match
# EXACTLY. The image provides the browser binaries; pip provides the driver
# that talks to them, and a mismatched pair fails at launch with an unhelpful
# error. Bump both together or neither.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# tzdata so zoneinfo can resolve TZ=America/Chicago. Without it the scheduler
# silently falls back to UTC and your "3:15am" run happens at 10:15pm.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright==1.58.0

WORKDIR /app

# Only the code. .env is deliberately NOT copied -- it arrives at runtime via
# env_file, so credentials never get baked into an image layer where they
# would persist even after the file is deleted. .dockerignore enforces this.
COPY silver_report.py notify.py scheduler.py ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=America/Chicago \
    RUN_AT=03:15

# Bind-mounted from the host. history.csv, exports/ and the logs live here so
# they survive `docker compose build` -- the history file is the one thing in
# this project that cannot be regenerated.
VOLUME ["/data"]

CMD ["python", "scheduler.py"]
