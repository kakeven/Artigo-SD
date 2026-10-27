FROM python:3.12-slim

WORKDIR /app
COPY req.txt .
RUN pip install --no-cache-dir -r req.txt
COPY *.py .
RUN mkdir -p /data
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
