FROM python:3.12-slim
WORKDIR /app
COPY 15m-breakout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY 15m-breakout/app/ ./app/
COPY 15m-breakout/main.py 15m-breakout/spec.json 15m-breakout/config.toml ./
COPY 15m-breakout/data/ ./data/
COPY 15m-breakout/blacklist.txt .
COPY 15m-breakout/whitelist.txt .
CMD ["python", "main.py"]
