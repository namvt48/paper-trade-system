FROM python:3.12-slim
WORKDIR /app
COPY 1d-kertrend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-kertrend/app/ ./app/
COPY 1d-kertrend/main.py 1d-kertrend/spec.json 1d-kertrend/config.toml ./
COPY 1d-kertrend/data/ ./data/
COPY 1d-kertrend/blacklist.txt .
COPY 1d-kertrend/whitelist.txt .
CMD ["python", "main.py"]
