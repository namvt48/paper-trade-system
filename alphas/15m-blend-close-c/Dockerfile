FROM python:3.12-slim
WORKDIR /app
COPY 15m-blend-close-c/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-blend-close-c/app/ ./app/
COPY 15m-blend-close-c/main.py 15m-blend-close-c/spec.json 15m-blend-close-c/config.toml ./
COPY 15m-blend-close-c/data/ ./data/
COPY 15m-blend-close-c/blacklist.txt .
COPY 15m-blend-close-c/whitelist.txt .
CMD ["python", "main.py"]
