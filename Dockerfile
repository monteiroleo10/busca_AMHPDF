FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git xvfb \
    && rm -rf /var/lib/apt/lists/*

# Extensao 2captcha para Chromium
RUN git clone --depth=1 https://github.com/2captcha/2captcha-solver /opt/2captcha-ext

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

EXPOSE 8501

CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
