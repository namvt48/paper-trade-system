FROM python:3.12-slim
WORKDIR /app
COPY alpha2-NR-Long-Short-H1/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha2-NR-Long-Short-H1/app/ ./app/
COPY alpha2-NR-Long-Short-H1/main.py .
CMD ["python", "main.py"]
