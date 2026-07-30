FROM python:3.12-slim
WORKDIR /app
COPY 1d-trend60cmf/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-trend60cmf/app/ ./app/
COPY 1d-trend60cmf/main.py 1d-trend60cmf/spec.json 1d-trend60cmf/config.toml ./
COPY 1d-trend60cmf/data/ ./data/
COPY 1d-trend60cmf/blacklist.txt .
COPY 1d-trend60cmf/whitelist.txt .
CMD ["python", "main.py"]
