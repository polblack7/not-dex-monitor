FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV FLASH_LOAN_ABI_PATH=/app/artifacts/FlashLoan.json

CMD ["python", "-m", "not_dex_monitor.main"]
