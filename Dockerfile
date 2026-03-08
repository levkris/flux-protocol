FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLUX_SECRET=""
ENV FLUX_BACKEND="sqlite"
ENV FLUX_DB_PATH="/data/flux.db"
ENV FLUX_ACCOUNTS_DB="/data/flux_accounts.db"
ENV FLUX_PORT="8765"
ENV FLUX_HOST="0.0.0.0"
ENV FLUX_DOMAIN=""

VOLUME ["/data"]
EXPOSE 8765

CMD ["sh", "-c", "python main.py server \
  --host $FLUX_HOST \
  --port $FLUX_PORT \
  --backend $FLUX_BACKEND \
  --db $FLUX_DB_PATH \
  --accounts-db $FLUX_ACCOUNTS_DB \
  $([ -n \"$FLUX_DOMAIN\" ] && echo \"--domain $FLUX_DOMAIN\")"]
