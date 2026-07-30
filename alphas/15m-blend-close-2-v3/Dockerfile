FROM python:3.12-slim
WORKDIR /app
COPY 15m-blend-close-2-v3/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-blend-close-2-v3/app/ ./app/
COPY 15m-blend-close-2-v3/main.py 15m-blend-close-2-v3/spec.json 15m-blend-close-2-v3/config.toml ./
COPY 15m-blend-close-2-v3/data/ ./data/
COPY 15m-blend-close-2-v3/blacklist.txt .
COPY 15m-blend-close-2-v3/whitelist.txt .
CMD ["python", "main.py"]
