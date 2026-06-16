FROM python:3.12-slim
WORKDIR /app
COPY 1d-blend-close/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-blend-close/app/ ./app/
COPY 1d-blend-close/main.py 1d-blend-close/spec.json 1d-blend-close/config.toml ./
COPY 1d-blend-close/data/ ./data/
COPY 1d-blend-close/blacklist.txt .
CMD ["python", "main.py"]
