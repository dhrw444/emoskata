FROM python:3.12-alpine

WORKDIR /app
COPY . /app

ENV TZ=Asia/Shanghai \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python3", "app.py"]
