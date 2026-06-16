FROM python:3.12-slim
WORKDIR /app
COPY 4h-trend-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 4h-trend-close/app/ ./app/
COPY 4h-trend-close/main.py 4h-trend-close/spec.json 4h-trend-close/config.toml ./
COPY 4h-trend-close/data/ ./data/
COPY 4h-trend-close/blacklist.txt .
CMD ["python", "main.py"]
