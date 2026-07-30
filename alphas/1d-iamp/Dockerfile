FROM python:3.12-slim
WORKDIR /app
COPY 1d-iamp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-iamp/app/ ./app/
COPY 1d-iamp/main.py 1d-iamp/spec.json 1d-iamp/config.toml ./
COPY 1d-iamp/data/ ./data/
COPY 1d-iamp/blacklist.txt .
COPY 1d-iamp/whitelist.txt .
CMD ["python", "main.py"]
