# Tesseract (OCR for scanned invoices) and Poppler (PDF -> image) are system packages,
# which is why this runs as a container rather than a plain Python host.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
