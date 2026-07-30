FROM python:3.12-slim
WORKDIR /app
COPY 4h-trend-close-v3/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 4h-trend-close-v3/app/ ./app/
COPY 4h-trend-close-v3/main.py 4h-trend-close-v3/spec.json 4h-trend-close-v3/config.toml ./
COPY 4h-trend-close-v3/data/ ./data/
COPY 4h-trend-close-v3/blacklist.txt .
COPY 4h-trend-close-v3/whitelist.txt .
CMD ["python", "main.py"]
