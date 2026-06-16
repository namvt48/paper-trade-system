FROM python:3.12-slim
WORKDIR /app
COPY 4h-trend-vwap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 4h-trend-vwap/app/ ./app/
COPY 4h-trend-vwap/main.py 4h-trend-vwap/spec.json 4h-trend-vwap/config.toml ./
COPY 4h-trend-vwap/data/ ./data/
COPY 4h-trend-vwap/blacklist.txt .
CMD ["python", "main.py"]
