# PostgreSQL Backup Tooling

The backup commands use PostgreSQL custom-format archives and never print the
database URL or password. Set `DATABASE_URL` in the operator environment. In
production, also set `AGE_RECIPIENT`; production backups refuse to run without
encryption.

```bash
# Set DATABASE_URL through the protected operator environment; do not put it
# in this file or pass it on a command line.
export APP_ENV=production
export AGE_RECIPIENT='age1...'
scripts/backup-db.sh --output-dir /secure/backups
scripts/backup-status.sh --backup-dir /secure/backups --json
```

For a disposable development database only, an unencrypted backup requires an
explicit flag and prints a warning:

```bash
APP_ENV=development scripts/backup-db.sh \
  --allow-test-database --allow-unencrypted-dev --output-dir /tmp/backups
```

Restore requires an explicit logical target database name. It first validates
the manifest, checksum, decryption, and `pg_restore --list`. Non-empty targets
are refused unless `--allow-destructive` is supplied. Production restores also
require `--confirm-production-restore`.

```bash
export AGE_IDENTITY_FILE=/secure/age/restore-identity.txt
scripts/restore-db.sh /secure/backups/infinityscan-prod.dump.age \
  --target-database infinityscan
```

Generated artifacts under `backups/` and `*.dump` files are gitignored.
