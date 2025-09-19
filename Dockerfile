FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your code
COPY . .

# Expose port 8080 (required for Cloud Functions)
EXPOSE 8080

# Run the app with uvicorn (for containerized deployment)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]