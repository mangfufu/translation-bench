# Deployment Checklist

Before starting the deployment, confirm that the backup completed successfully.

> Do not restart the database while a migration is running.

## Required steps

1. Open the `config.toml` file and verify the environment name.
2. Check the status page at https://status.example.com before continuing.
3. Run `appctl validate --strict` and review every warning.
4. Notify the support team that maintenance has started.

## Verification

- [ ] The API returns HTTP 200.
- [ ] Existing users can sign in.
- [ ] A new user can complete registration.
- [ ] Background jobs are processing normally.

The values `${APP_PORT}`, `MAX_RETRIES=5`, and `v2.4.1` must remain unchanged.

```bash
appctl deploy --environment production
appctl status --watch
```

If validation fails, stop the deployment and restore the latest known-good release.
