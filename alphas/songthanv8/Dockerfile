FROM python:3.12-slim
WORKDIR /app
COPY songthanv8/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY indicators/ ./indicators/
COPY songthanv8/app/ ./app/
COPY songthanv8/main.py songthanv8/spec.json songthanv8/config.toml ./
COPY songthanv8/data/ ./data/
COPY songthanv8/blacklist.txt .
CMD ["python", "main.py"]
