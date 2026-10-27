FROM python:3.8.18-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libc-dev \
        libpq-dev \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install build tooling first.
# setuptools 75.3.4 is the last setuptools release supporting Python 3.8.
RUN python -m pip install --no-cache-dir \
    "pip==24.0" \
    "setuptools==75.3.4" \
    "wheel==0.45.1"

# Copy dependency metadata/source separately so this layer remains cacheable
# when only the sample application's code changes.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY sample-app/requirements.txt ./requirements.txt

# This is a development/sample image, so install tp_trace editable.
# Build dependencies are already installed above, avoiding pip's temporary
# isolated build environment.
RUN python -m pip install --no-cache-dir \
    --no-build-isolation \
    -e . \
    -r requirements.txt

COPY sample-app/api ./api/
COPY sample-app/config ./config/
COPY sample-app/manage.py \
     sample-app/gunicorn.conf.py \
     sample-app/userlist.txt \
     ./

RUN TP_TRACE_DISABLED=true python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]
