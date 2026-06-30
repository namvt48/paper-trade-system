FROM python:3.12-slim
WORKDIR /app
COPY 1h-decay-close-v3/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-decay-close-v3/app/ ./app/
COPY 1h-decay-close-v3/main.py 1h-decay-close-v3/spec.json 1h-decay-close-v3/config.toml ./
COPY 1h-decay-close-v3/data/ ./data/
COPY 1h-decay-close-v3/blacklist.txt .
CMD ["python", "main.py"]
