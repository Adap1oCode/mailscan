FROM python:3.12-slim

# System deps: Tesseract OCR + libdmtx for Mailmark barcode decode
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libdmtx0b \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV TESSERACT_CMD=/usr/bin/tesseract
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
