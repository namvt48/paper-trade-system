FROM python:3.12-slim

WORKDIR /app

COPY alphas/runner/requirements.txt /tmp/pm-requirements.txt
RUN pip install --no-cache-dir -r /tmp/pm-requirements.txt

COPY alphas/ /app/alphas
COPY indicators/ /app/indicators
COPY portfolio_manager/ /app/portfolio_manager

ENV PYTHONPATH=/app/alphas:/app
ENV PORTFOLIO_CONFIG=/app/portfolio_manager/config/portfolio.json

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
  CMD python -c "import json,os; json.load(open(os.environ['PORTFOLIO_CONFIG']))"

CMD ["python", "-m", "portfolio_manager.main"]
