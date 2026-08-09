FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schema.sql importer.py main.py ./
COPY static ./static

EXPOSE 8000

# import (idempotent, skips when up to date) then serve
CMD ["sh", "-c", "python importer.py && uvicorn main:app --host 0.0.0.0 --port 8000"]
