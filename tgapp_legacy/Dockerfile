FROM python:3.9-alpine

COPY requirements.txt /

RUN pip install --no-cache-dir -r /requirements.txt

COPY app /app

WORKDIR /

CMD PYTHONPATH=. python app/bot.py
