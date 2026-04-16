FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libglib2.0-0 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcairo2 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libpangocairo-1.0-0 libasound2 libnspr4 libnss3 libxss1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 wget fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install chromium

COPY . .

CMD ["python", "bot.py"]
