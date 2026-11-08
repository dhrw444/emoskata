FROM python:3.12-alpine

WORKDIR /app
COPY emoskata.py /app/

# 时区数据（缺了会退回固定偏移表，装了才准）
RUN apk add --no-cache tzdata && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

ENV TZ=Asia/Shanghai \
    EMOS_ENV_FILE=/app/config/.env \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/app/data", "/app/config"]
EXPOSE 8080

CMD ["python3", "emoskata.py"]
