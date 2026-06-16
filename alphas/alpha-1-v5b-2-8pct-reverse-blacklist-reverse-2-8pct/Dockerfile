FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-v5b-2-8pct-reverse-blacklist-reverse-2-8pct/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-v5b-2-8pct-reverse-blacklist-reverse-2-8pct/app/ ./app/
COPY alpha-1-v5b-2-8pct-reverse-blacklist-reverse-2-8pct/main.py .
CMD ["python", "main.py"]
