FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
EXPOSE 8080
# timeout-graceful-shutdown: force-close long-lived SSE connections on stop
# so docker stop does not wait for clients to disconnect.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "2"]
