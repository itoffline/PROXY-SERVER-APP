FROM python:3.12-alpine

RUN apk add --no-cache git

WORKDIR /app

# Установка зависимостей FastAPI
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование всех необходимых файлов
COPY server.py start.sh ./

RUN chmod +x /app/start.sh
RUN mkdir -p /data

EXPOSE 8000 8080
