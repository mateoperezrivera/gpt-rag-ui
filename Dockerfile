# Use Python 3.12 slim base image
FROM python:3.12-slim

# Set app dir
WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy app code
COPY . .

# Expose port 80
EXPOSE 80

# Launch via Uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 80 || sleep 3600"]
