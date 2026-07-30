FROM python:3.12-slim
WORKDIR /app
COPY 15m-blend-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-blend-close/app/ ./app/
COPY 15m-blend-close/main.py 15m-blend-close/spec.json 15m-blend-close/config.toml ./
COPY 15m-blend-close/data/ ./data/
COPY 15m-blend-close/blacklist.txt .
COPY 15m-blend-close/whitelist.txt .
CMD ["python", "main.py"]
