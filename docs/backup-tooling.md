# Database and Object Backups

The tooling uses PostgreSQL custom-format dumps, JSON manifests, SHA-256 and
byte-size checks, and optional age encryption. It never prints the database URL
or password. Supply credentials through a protected operator environment, not
command-line arguments.

## Formats

- v1 is database-only and remains supported by backup status and restore.
  `--database-only` selects it explicitly; omitting a mode also produces v1 in
  non-production environments for compatibility.
- v2 is a complete database and referenced-object backup. `--objects` and
  `--complete` are aliases. It publishes a database artifact, an object tar
  sidecar, and one manifest describing and checksumming both.

v2 takes the database dump from an exported PostgreSQL snapshot. It refuses to
start while any import is `pending`, `scanning`, `uploading`, or `verifying`,
then verifies every referenced object against its expected content, size, MIME
type, and stable ETag while creating the sidecar.

## Production backup

Production requires `APP_ENV=production`, age encryption, and an explicit
`--complete`/`--objects` or `--database-only` mode. A complete backup also
requires enabled, healthy object-storage settings, including the source
`S3_BUCKET`.

```bash
export APP_ENV=production
export DATABASE_URL='postgresql+psycopg://...'
export AGE_RECIPIENT='age1...'
export OBJECT_STORAGE_ENABLED=true
# Export the protected S3_ENDPOINT_URL, S3_REGION, S3_BUCKET, and credentials.

scripts/backup-db.sh --complete --output-dir /secure/backups
scripts/backup-status.sh --backup-dir /secure/backups --json
```

`backup-status.sh` validates manifests and checks database and object artifacts
independently. It reports `ok`, `missing_artifact`, `checksum_mismatch`, or
`invalid_manifest`, and exits nonzero if any discovered backup is not `ok`.

For a disposable development database, unencrypted output requires an explicit
warning-producing override. Use `--allow-test-database` only when the logical
database name is intentionally a test/temporary name:

```bash
APP_ENV=development scripts/backup-db.sh --objects \
  --allow-test-database --allow-unencrypted-dev --output-dir /tmp/backups
```

`--allow-unencrypted-dev` and `--allow-test-database` are forbidden in
production.

## Restore

The restore tool does not create databases or buckets. Create a fresh target
database and target bucket first, configure `DATABASE_URL` for that exact
database, and configure the object-storage endpoint and credentials for the
target environment. A v2 restore requires `--target-bucket`; v1 does not.

```bash
export APP_ENV=production
export DATABASE_URL='postgresql+psycopg://.../infinityscan_restore'
export AGE_IDENTITY_FILE=/secure/age/restore-identity.txt
export OBJECT_STORAGE_ENABLED=true
# Export the protected target S3 endpoint, region, and credentials.

scripts/restore-db.sh /secure/backups/infinityscan-prod.dump.age \
  --target-database infinityscan_restore \
  --target-bucket infinityscan-restore \
  --confirm-production-restore
```

Before mutation, restore validates the manifest, both v2 artifact checksums and
sizes, age decryption, every tar member, and `pg_restore --list`. The connected
database name must exactly equal `--target-database`; a non-empty database is
refused unless `--allow-destructive` is explicit. Production additionally
requires `--confirm-production-restore`.

For each archived object, a missing target key is uploaded, an exact content,
size, and MIME match is reused, and a mismatch aborts without overwriting that
key. Unrelated target-bucket objects are not removed. Objects are prepared
before the single-transaction database restore, then restored page and import
records are rebound to the destination ETags. Use a fresh bucket because the
database and object store do not share one transaction.

## Disaster-recovery drill

```bash
scripts/test-disaster-recovery.sh
```

The drill uses a unique local Compose project and genuinely distinct source and
fresh restore database/bucket targets. It creates and status-checks an encrypted
v2 backup, restores it, verifies schema, relationships, API behavior, and full
object SHA-256 content, and proves the source remains intact. It then runs a
real import worker against the restored targets, sends that exact lock-owning
process `SIGKILL` after its first upload, requires exit 137, and verifies
lease-aware safe recovery after expiry. It refuses production mode and remote
Docker by default.

Generated `backups/` directories, dump artifacts, manifests, and v2 object tar
sidecars (including age-encrypted variants) are gitignored.
