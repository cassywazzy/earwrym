FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates xvfb procps \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pydoll-python

WORKDIR /app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
COPY poller.py .

CMD ["./entrypoint.sh"]
