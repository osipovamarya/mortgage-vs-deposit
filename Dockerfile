FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY web/requirements.txt ./web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

# Copy the whole repo so web/app can reference root app/ if needed later
COPY . .

ENV DB_PATH=/app/db/mortgage_web.db
ENV PYTHONPATH=/app/web

EXPOSE 5000

CMD ["python", "-m", "flask", "--app", "web/app/main.py", "run", "--host=0.0.0.0", "--port=5000"]
