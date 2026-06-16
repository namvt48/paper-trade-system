FROM python:3.12-slim

WORKDIR /app/alphas

COPY runner/requirements.txt /tmp/runner-requirements.txt
RUN pip install --no-cache-dir -r /tmp/runner-requirements.txt

COPY . /app/alphas

ENV PYTHONPATH=/app/alphas
ENV RUNNER_METRICS_HOST=0.0.0.0
ENV RUNNER_METRICS_PORT=9091

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('RUNNER_METRICS_PORT','9091'), timeout=3).read()"

CMD ["python", "-m", "runner.main"]
