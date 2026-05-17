FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY earwrym/ earwrym/
COPY static/ static/
COPY config.example.yaml .

ENV EARWRYM_CONFIG=/data/config.yaml
ENV EARWRYM_DB=/data/earwrym.db
ENV EARWRYM_PORT=8587

VOLUME /data
EXPOSE 8587

CMD ["python", "-m", "earwrym"]
