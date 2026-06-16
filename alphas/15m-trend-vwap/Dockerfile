FROM python:3.12-slim
WORKDIR /app
COPY 15m-trend-vwap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-trend-vwap/app/ ./app/
COPY 15m-trend-vwap/main.py 15m-trend-vwap/spec.json 15m-trend-vwap/config.toml ./
COPY 15m-trend-vwap/data/ ./data/
COPY 15m-trend-vwap/blacklist.txt .
CMD ["python", "main.py"]
