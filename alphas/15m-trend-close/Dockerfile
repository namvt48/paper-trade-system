FROM python:3.12-slim
WORKDIR /app
COPY 15m-trend-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-trend-close/app/ ./app/
COPY 15m-trend-close/main.py 15m-trend-close/spec.json 15m-trend-close/config.toml ./
COPY 15m-trend-close/data/ ./data/
COPY 15m-trend-close/blacklist.txt .
CMD ["python", "main.py"]
