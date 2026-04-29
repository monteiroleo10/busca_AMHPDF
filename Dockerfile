FROM python:3.11-slim

# Dependencias do sistema para o Firefox
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libgtk-3-0 libdbus-glib-1-2 libxt6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install firefox
RUN playwright install-deps firefox
RUN pip install --no-cache-dir playwright-stealth requests beautifulsoup4

COPY . .

EXPOSE 8501

CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
