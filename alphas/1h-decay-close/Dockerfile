FROM python:3.12-slim
WORKDIR /app
COPY 1h-decay-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-decay-close/app/ ./app/
COPY 1h-decay-close/main.py 1h-decay-close/spec.json 1h-decay-close/config.toml ./
COPY 1h-decay-close/data/ ./data/
COPY 1h-decay-close/blacklist.txt .
COPY 1h-decay-close/whitelist.txt .
CMD ["python", "main.py"]
