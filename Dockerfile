FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY static ./static
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 SETKA_DATA_DIR=/data
EXPOSE 8080
CMD ["python", "server.py"]
