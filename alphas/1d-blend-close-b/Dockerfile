FROM python:3.12-slim
WORKDIR /app
COPY 1d-blend-close-b/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-blend-close-b/app/ ./app/
COPY 1d-blend-close-b/main.py 1d-blend-close-b/spec.json 1d-blend-close-b/config.toml ./
COPY 1d-blend-close-b/data/ ./data/
COPY 1d-blend-close-b/blacklist.txt .
CMD ["python", "main.py"]
