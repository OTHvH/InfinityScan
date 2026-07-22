# Security dependency overrides

## Next.js 16.2.10 image and CSS dependencies

Next.js 16.2.10 declares `sharp@^0.34.5` and pins `postcss@8.4.31`.
Those ranges cannot resolve the patched releases for GHSA-f88m-g3jw-g9cj
and GHSA-qx2v-qp2m-jg93. Next.js 16.2.11 retains the same dependency
constraints, so a patch-line Next.js update does not remediate either issue.

`package.json` therefore installs `sharp@0.35.3` directly and narrowly
overrides the two Next.js children to `sharp@0.35.3` and `postcss@8.5.10`.
Next.js uses Sharp's stable constructor, concurrency, resize, rotate, encoder,
timeout, and `toBuffer` APIs, which remain available in Sharp 0.35.3.

Review these overrides when upgrading Next.js. Remove each override once the
selected Next.js release natively requires the same or a newer patched version.
Compatibility is enforced by frontend tests, the production Next.js build,
the native Docker build, and the required ARM64 Docker build.
