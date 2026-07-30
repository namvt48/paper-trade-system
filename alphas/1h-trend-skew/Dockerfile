FROM python:3.12-slim
WORKDIR /app
COPY 1h-trend-skew/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY 1h-trend-skew/app/ ./app/
COPY 1h-trend-skew/main.py 1h-trend-skew/spec.json 1h-trend-skew/config.toml ./
COPY 1h-trend-skew/data/ ./data/
COPY 1h-trend-skew/blacklist.txt .
COPY 1h-trend-skew/whitelist.txt .
CMD ["python", "main.py"]
