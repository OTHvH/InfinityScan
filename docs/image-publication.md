# Immutable Image Publication

`.github/workflows/publish-images.yml` is the only workflow that publishes
application images. It is manually dispatched with the run ID of a successful
`Full Release Verification` run for the protected
`phase-6-production-deployment` branch. The workflow is also protected by the
`image-publication` environment. Pull requests, forks, arbitrary pushes, and
local developer environments cannot publish or sign images.

## Image identity

The workflow derives lowercase names from repository metadata:

```text
ghcr.io/<owner>/infinityscan-api
ghcr.io/<owner>/infinityscan-web
```

Each image is built with Buildx for `linux/amd64` and `linux/arm64`. The only
informational tag is `sha-<full-git-sha>`. Deployment never consumes that tag;
it consumes the manifest digest:

```text
ghcr.io/<owner>/infinityscan-api@sha256:<64-hex-digest>
ghcr.io/<owner>/infinityscan-web@sha256:<64-hex-digest>
```

The workflow never publishes `latest`, `main`, `master`, `production`, or
`stable` aliases.

## Verification and attestations

Before publishing, the workflow consumes the exact successful verification run,
checks the Dockerfiles and lock files, runs source checks, builds local runtime
images, and rejects secret-looking content and root users. After publishing it
scans each digest with Trivy, generates SPDX JSON SBOMs from the final images,
adds GitHub build provenance attestations, signs each digest with keyless
Cosign OIDC signing, verifies both platforms, and validates the release
manifest.

Buildx provenance records the source revision, Dockerfile, builder, and target
platforms. GitHub attestations bind the image digest to this repository and
workflow. They do not attest the production VM, external PostgreSQL, R2
configuration, runtime secrets, or deployment success.

The checked-in verifier can be used by a deployment or staging workflow:

```bash
scripts/verify-release-images.sh \
  --manifest release-manifest.json \
  --expected-repository OWNER/InfinityScan \
  --expected-workflow-identity \
  'https://github.com/OWNER/InfinityScan/.github/workflows/publish-images.yml@refs/heads/phase-6-production-deployment' \
  --expected-git-sha <40-hex-commit>
```

The verifier checks the registry manifest, both architectures, keyless
signature identity and issuer, GitHub provenance, SBOM presence, and the scan
summary. Use `--offline` only for manifest and local fixture validation; it
does not verify registry objects or signatures.

The workflow artifact is retained for 14 days and contains only the release
manifest, SBOMs, checksums, scan reports, and verification summaries. It has no
secrets, database data, tokens, or private keys. The same manifest can be
passed to deployment with `RELEASE_MANIFEST_FILE`; the deployment and rollback
paths still validate and use digest references only.
