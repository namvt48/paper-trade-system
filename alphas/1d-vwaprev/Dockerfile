FROM python:3.12-slim
WORKDIR /app
COPY 1d-vwaprev/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 1d-vwaprev/app/ ./app/
COPY 1d-vwaprev/main.py 1d-vwaprev/spec.json 1d-vwaprev/config.toml ./
COPY 1d-vwaprev/data/ ./data/
COPY 1d-vwaprev/blacklist.txt .
COPY 1d-vwaprev/whitelist.txt .
CMD ["python", "main.py"]
