FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY static/ static/
EXPOSE 8080
# timeout-graceful-shutdown: force-close long-lived SSE connections on stop
# so docker stop does not wait for clients to disconnect.
# PORT picks the listen port. exec keeps uvicorn as PID 1 for SIGTERM.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 2"]
