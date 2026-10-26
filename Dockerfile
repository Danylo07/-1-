FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN useradd --create-home sim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY devices.py .
USER sim
CMD ["python", "devices.py", "--scenario", "normal"]
