FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY studio ./studio
COPY static ./static
COPY run.py .
RUN useradd --uid 10001 --create-home studio && mkdir -p /app/data && chown studio:studio /app/data
USER studio
EXPOSE 8780
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8780"]
