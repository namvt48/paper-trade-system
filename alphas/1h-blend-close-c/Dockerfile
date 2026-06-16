FROM python:3.12-slim
WORKDIR /app
COPY 1h-blend-close-c/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1h-blend-close-c/app/ ./app/
COPY 1h-blend-close-c/main.py 1h-blend-close-c/spec.json 1h-blend-close-c/config.toml ./
COPY 1h-blend-close-c/data/ ./data/
COPY 1h-blend-close-c/blacklist.txt .
CMD ["python", "main.py"]
