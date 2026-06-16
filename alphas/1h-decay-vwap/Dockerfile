FROM python:3.12-slim
WORKDIR /app
COPY 1h-decay-vwap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-decay-vwap/app/ ./app/
COPY 1h-decay-vwap/main.py 1h-decay-vwap/spec.json 1h-decay-vwap/config.toml ./
COPY 1h-decay-vwap/data/ ./data/
COPY 1h-decay-vwap/blacklist.txt .
CMD ["python", "main.py"]
