FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Validate that GitHub upload included the UI before the container starts.
RUN test -f /app/static/index.html && test -f /app/static/app.js && test -f /app/static/style.css
RUN mkdir -p /app/data/photos
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
