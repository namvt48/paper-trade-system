FROM python:3.12-slim
WORKDIR /app
COPY alpha-1-q2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha-1-q2/app/ ./app/
COPY alpha-1-q2/main.py .
CMD ["python", "main.py"]
