# Imagem so para o servidor Slack (sem Playwright)
FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements-server.txt ./requirements-server.txt
RUN pip install --no-cache-dir -r requirements-server.txt

COPY server ./server

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
