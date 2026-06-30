FROM python:3.12-slim
WORKDIR /app
COPY alpha2-NR-Long-Short-M15/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY alpha2-NR-Long-Short-M15/app/ ./app/
COPY alpha2-NR-Long-Short-M15/main.py .
CMD ["python", "main.py"]
