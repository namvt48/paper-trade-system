FROM python:3.12-slim
WORKDIR /app
COPY adx-trend-follow/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY adx-trend-follow/app/ ./app/
COPY adx-trend-follow/main.py .
CMD ["python", "main.py"]
