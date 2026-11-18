# Admin account setup

The registration endpoint intentionally assigns the `user` role to everyone.

For a fresh local test database, first register your own account in the UI. Then connect to PostgreSQL:

```bash
docker compose -f deploy/compose/docker-compose.yml exec postgres psql -U raguser -d ragdb
```

Promote only a trusted account (replace the email):
```sql
UPDATE users SET role = 'admin' WHERE email = 'you@example.com';
```

Log out and log in again so that a new JWT contains the admin role. The API checks the role stored in the database on each request.

Never expose an unauthenticated role-promotion endpoint. In production, use a controlled administrative bootstrap process and audited role changes.
