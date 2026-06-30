FROM python:3.12-slim
WORKDIR /app
COPY deobietcophaialphakhong/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY deobietcophaialphakhong/app/ ./app/
COPY deobietcophaialphakhong/main.py .
CMD ["python", "main.py"]
