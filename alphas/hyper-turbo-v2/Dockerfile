FROM python:3.12-slim
WORKDIR /app
COPY hyper-turbo-v2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY hyper-turbo-v2/app/ ./app/
COPY hyper-turbo-v2/main.py .
CMD ["python", "main.py"]
