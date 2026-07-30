FROM python:3.12-slim
WORKDIR /app
COPY 4h-trend-z/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY 4h-trend-z/app/ ./app/
COPY 4h-trend-z/main.py 4h-trend-z/spec.json 4h-trend-z/config.toml ./
COPY 4h-trend-z/data/ ./data/
COPY 4h-trend-z/blacklist.txt .
COPY 4h-trend-z/whitelist.txt .
CMD ["python", "main.py"]
