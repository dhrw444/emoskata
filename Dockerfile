FROM python:3.12-alpine

WORKDIR /app
COPY emoskata.py /app/

# 时区数据（缺了会退回固定偏移表，装了才准）
RUN apk add --no-cache tzdata && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

ENV TZ=Asia/Shanghai \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python3", "emoskata.py"]
