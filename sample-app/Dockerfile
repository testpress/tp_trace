FROM python:3.8.18-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
  libpq-dev \
  gcc \
  python3-dev \
  libc-dev \
  && rm -rf /var/lib/apt/lists/*

# tp_trace SDK (editable install from repo root build context)
COPY pyproject.toml .
COPY README.md .
COPY src/ ./src/

# Sample app requirements
COPY sample-app/requirements.txt .

RUN python -m pip install --upgrade "pip<24.1" \
  && python -m pip install -e . \
  && python -m pip install -r requirements.txt

# Sample app source
COPY sample-app/api ./api
COPY sample-app/config ./config
COPY sample-app/manage.py ./manage.py
COPY sample-app/gunicorn.conf.py ./gunicorn.conf.py
COPY sample-app/userlist.txt ./userlist.txt

RUN TP_TRACE_DISABLED=true python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]
