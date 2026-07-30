FROM python:3.12-slim
WORKDIR /app
COPY songthanv11/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY songthanv11/app/ ./app/
COPY songthanv11/main.py songthanv11/spec.json songthanv11/config.toml ./
COPY songthanv11/data/ ./data/
COPY songthanv11/blacklist.txt .
COPY songthanv11/whitelist.txt .
CMD ["python", "main.py"]
