FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY bot.py /app/bot.py
WORKDIR /app

# state.json + session live in DATA_DIR. Attach a Railway Volume mounted
# at /data (dashboard or `railway volumes`) to persist across deploys;
# without one, state is recreated on each deploy.
ENV DATA_DIR=/data

CMD ["python", "bot.py"]
