FROM python:3.12-slim
WORKDIR /app
COPY 1h-trend-breakout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY 1h-trend-breakout/app/ ./app/
COPY 1h-trend-breakout/main.py 1h-trend-breakout/spec.json 1h-trend-breakout/config.toml ./
COPY 1h-trend-breakout/data/ ./data/
COPY 1h-trend-breakout/blacklist.txt .
CMD ["python", "main.py"]
