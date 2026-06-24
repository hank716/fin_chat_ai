# Qlib 隔離離線 image（階段 3 [8]）——pyqlib 重且 Python 版本挑剔，故獨立成 image，
# 與 backend / scheduler / bot 容器完全隔離；只讀掛載的 /data 與 /app/configs，寫 JSON 回 /data。
FROM python:3.11-slim

# lightgbm 執行期需要 libgomp；pyqlib 編譯 Cython 擴充需要 build-essential。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# runtime 資料掛 /data（= repo storage/），與 backend 同慣例。
ENV LOCAL_STORAGE_PATH=/data
ENV PYTHONUNBUFFERED=1

# 預設：啟動跑一次 eval+score，之後 off-peak nightly 內部 loop 重跑（不需 scheduler/docker exec）。
ENTRYPOINT ["python", "run.py"]
CMD ["--loop"]
