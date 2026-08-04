# Security dependency overrides

## Next.js image and CSS dependencies

Next.js 16.2.12 declares `sharp@^0.34.5` and pins `postcss@8.4.31`.
Those ranges cannot resolve the patched releases required by the current npm
advisories, so updating Next.js alone does not remediate either dependency.

`package.json` therefore installs `sharp@0.35.3` directly and narrowly
overrides the two Next.js children to `sharp@0.35.3` and `postcss@8.5.23`.
Next.js uses Sharp's stable constructor, concurrency, resize, rotate, encoder,
timeout, and `toBuffer` APIs, which remain available in Sharp 0.35.3.

Review these overrides when upgrading Next.js. Remove each override once the
selected Next.js release natively requires the same or a newer patched version.
Compatibility is enforced by frontend tests, the production Next.js build,
the native Docker build, and the required ARM64 Docker build.

The lockfile also overrides the transitive `undici` package to `7.29.0` until
the parent tooling packages require that patched release directly.

## ESLint glob dependency

The ESLint 9 dependency graph still requests older `brace-expansion` release
lines through `minimatch`. All minimatch versions are pinned to the direct,
patched `brace-expansion@5.0.9` development dependency. Because minimatch 3
requires the old callable CommonJS export, the idempotent postinstall script
adds that compatibility shape to the installed CommonJS entry point while
retaining all modern named exports and the patched implementation. The script
fails if the exact expected package cannot be verified. This avoids an
incompatible ESLint major upgrade. Frontend lint and tests enforce compatibility.
