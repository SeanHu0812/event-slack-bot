FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# -u = unbuffered stdout/stderr so logs stream to the platform in real time
CMD ["python", "-u", "app.py"]
