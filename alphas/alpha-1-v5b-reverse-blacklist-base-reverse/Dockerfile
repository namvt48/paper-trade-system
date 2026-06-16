FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-v5b-reverse-blacklist-base-reverse/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-v5b-reverse-blacklist-base-reverse/app/ ./app/
COPY alpha-1-v5b-reverse-blacklist-base-reverse/main.py .
CMD ["python", "main.py"]
