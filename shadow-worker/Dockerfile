FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /tmp/shadow-worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/shadow-worker-requirements.txt
COPY . /app

CMD ["python", "main.py"]
