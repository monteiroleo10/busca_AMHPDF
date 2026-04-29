FROM python:3.11-slim-bookworm

RUN apt-get update && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

EXPOSE 8501

CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
