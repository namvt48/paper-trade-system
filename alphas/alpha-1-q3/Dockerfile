FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-q3/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-q3/app/ ./app/
COPY alpha-1-q3/main.py .
CMD ["python", "main.py"]
