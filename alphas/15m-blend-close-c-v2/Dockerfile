FROM python:3.12-slim
WORKDIR /app
COPY 15m-blend-close-c-v2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 15m-blend-close-c-v2/app/ ./app/
COPY 15m-blend-close-c-v2/main.py 15m-blend-close-c-v2/spec.json 15m-blend-close-c-v2/config.toml ./
COPY 15m-blend-close-c-v2/data/ ./data/
COPY 15m-blend-close-c-v2/blacklist.txt .
CMD ["python", "main.py"]
