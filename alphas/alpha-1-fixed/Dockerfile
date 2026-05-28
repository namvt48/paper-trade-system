FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-fixed/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-fixed/app/ ./app/
COPY alpha-1-fixed/main.py .
CMD ["python", "main.py"]
