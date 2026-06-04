FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-bangoc/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-bangoc/app/ ./app/
COPY alpha-1-bangoc/main.py .
CMD ["python", "main.py"]
