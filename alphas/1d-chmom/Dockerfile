FROM python:3.12-slim
WORKDIR /app
COPY 1d-chmom/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-chmom/app/ ./app/
COPY 1d-chmom/main.py 1d-chmom/spec.json 1d-chmom/config.toml ./
COPY 1d-chmom/data/ ./data/
COPY 1d-chmom/blacklist.txt .
COPY 1d-chmom/whitelist.txt .
CMD ["python", "main.py"]
