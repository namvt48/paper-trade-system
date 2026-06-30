FROM python:3.12-slim
WORKDIR /app
COPY 4h-amihud/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY 4h-amihud/app/ ./app/
COPY 4h-amihud/main.py 4h-amihud/spec.json 4h-amihud/config.toml ./
COPY 4h-amihud/data/ ./data/
COPY 4h-amihud/blacklist.txt .
CMD ["python", "main.py"]
