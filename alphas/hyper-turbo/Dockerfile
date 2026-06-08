FROM python:3.12-slim
WORKDIR /app
COPY hyper-turbo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY hyper-turbo/app/ ./app/
COPY hyper-turbo/main.py .
CMD ["python", "main.py"]
