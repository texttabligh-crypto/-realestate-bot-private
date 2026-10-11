# Security policy

- Keep the repository private.
- Never commit `.env`, Telegram/OpenAI tokens, database passwords, private keys,
  database dumps, customer data, production logs, or backups.
- Store production secrets only on the server. Use GitHub Actions secrets only
  for values that are strictly required by CI or deployment.
- Deployment access must be restricted to `/opt/realestate-bot` and must not
  operate on unrelated projects or containers.
- Every production deployment must create a backup, run smoke and health checks,
  and provide a tested rollback path.
