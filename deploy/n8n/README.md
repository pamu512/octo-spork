# Mac n8n bus

## Install n8n

```bash
cp deploy/n8n/.env.example deploy/n8n/.env.local
# set N8N_ENCRYPTION_KEY (e.g. openssl rand -hex 32) and Mac paths
docker compose -f deploy/n8n/docker-compose.yml --env-file deploy/n8n/.env.local up -d
```

Open http://localhost:5678
