FROM python:3.11-slim

WORKDIR /app

COPY auditor/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY auditor/ ./auditor/

CMD ["python", "-m", "auditor.bot.main"]
