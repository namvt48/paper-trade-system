FROM python:3.12-slim
WORKDIR /app
COPY 4h-momentum-vwap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY 4h-momentum-vwap/app/ ./app/
COPY 4h-momentum-vwap/main.py 4h-momentum-vwap/spec.json 4h-momentum-vwap/config.toml ./
COPY 4h-momentum-vwap/data/ ./data/
COPY 4h-momentum-vwap/blacklist.txt .
CMD ["python", "main.py"]
