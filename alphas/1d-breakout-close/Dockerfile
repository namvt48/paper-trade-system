FROM python:3.12-slim
WORKDIR /app
COPY 1d-breakout-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-breakout-close/app/ ./app/
COPY 1d-breakout-close/main.py 1d-breakout-close/spec.json 1d-breakout-close/config.toml ./
COPY 1d-breakout-close/data/ ./data/
COPY 1d-breakout-close/blacklist.txt .
CMD ["python", "main.py"]
