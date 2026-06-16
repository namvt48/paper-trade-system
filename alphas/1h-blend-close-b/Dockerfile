FROM python:3.12-slim
WORKDIR /app
COPY 1h-blend-close-b/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-blend-close-b/app/ ./app/
COPY 1h-blend-close-b/main.py 1h-blend-close-b/spec.json 1h-blend-close-b/config.toml ./
COPY 1h-blend-close-b/data/ ./data/
COPY 1h-blend-close-b/blacklist.txt .
CMD ["python", "main.py"]
