FROM python:3.9-slim

WORKDIR /app

# 安裝系統依賴 (如果需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 設置環境變量以確保 Python 輸出不被緩衝
ENV PYTHONUNBUFFERED=1

# 運行主程序
CMD ["python", "main.py"]
