FROM python:3.12-slim
WORKDIR /app
COPY 15m-blend-close-b/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-blend-close-b/app/ ./app/
COPY 15m-blend-close-b/main.py 15m-blend-close-b/spec.json 15m-blend-close-b/config.toml ./
COPY 15m-blend-close-b/data/ ./data/
COPY 15m-blend-close-b/blacklist.txt .
COPY 15m-blend-close-b/whitelist.txt .
CMD ["python", "main.py"]
