FROM python:3.12-alpine

WORKDIR /app
COPY emoskata.py /app/

# zoneinfo 依赖 IANA 时区数据库；缺失时应用只退回有限的固定偏移表。
RUN apk add --no-cache tzdata && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

ENV TZ=Asia/Shanghai \
    EMOS_ENV_FILE=/app/config/.env \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/app/data", "/app/config"]
EXPOSE 8080

CMD ["python3", "emoskata.py"]
