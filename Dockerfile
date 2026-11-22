FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kalshi_bot.py .
COPY kalshi_auth.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "kalshi_bot.py"]
