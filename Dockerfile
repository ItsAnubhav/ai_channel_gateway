FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /service

COPY . /service/channel_gateway
RUN pip install --no-cache-dir -e /service/channel_gateway

ENV PYTHONPATH=/service

EXPOSE 8010

CMD ["uvicorn", "channel_gateway.app.main:app", "--host", "0.0.0.0", "--port", "8010"]
