FROM python:3.12-slim
WORKDIR /app
COPY 1h-blend-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-blend-close/app/ ./app/
COPY 1h-blend-close/main.py 1h-blend-close/spec.json 1h-blend-close/config.toml ./
COPY 1h-blend-close/data/ ./data/
COPY 1h-blend-close/blacklist.txt .
CMD ["python", "main.py"]
