FROM node:24.18.0-alpine3.23@sha256:595398b0081eacda8e1c4c5b97b76cd1020e4d58a8ebcb4843b9bca1e79e7436 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
COPY scripts ./scripts
RUN npm ci

FROM node:24.18.0-alpine3.23@sha256:595398b0081eacda8e1c4c5b97b76cd1020e4d58a8ebcb4843b9bca1e79e7436 AS builder
WORKDIR /app
ARG APP_ENV=development
ENV APP_ENV=$APP_ENV
ARG MEDIA_CSP_ORIGINS
ENV MEDIA_CSP_ORIGINS=$MEDIA_CSP_ORIGINS
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG API_INTERNAL_URL
ENV API_INTERNAL_URL=$API_INTERNAL_URL
RUN npm run build

FROM node:24.18.0-alpine3.23@sha256:595398b0081eacda8e1c4c5b97b76cd1020e4d58a8ebcb4843b9bca1e79e7436 AS runner
WORKDIR /app
ARG APP_ENV=development
ARG API_INTERNAL_URL
ARG MEDIA_CSP_ORIGINS
ENV APP_ENV=$APP_ENV
ENV API_INTERNAL_URL=$API_INTERNAL_URL
ENV MEDIA_CSP_ORIGINS=$MEDIA_CSP_ORIGINS
ENV NODE_ENV=production
ENV HOSTNAME=0.0.0.0

# Create non-root user for security
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# The standalone server does not need package-management tooling at runtime.
RUN rm -rf /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# Set ownership for Next.js
RUN chown nextjs:nodejs /app

USER nextjs

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "require('http').get('http://localhost:3000', r => r.statusCode === 200 ? process.exit(0) : process.exit(1)).on('error', () => process.exit(1))"]

EXPOSE 3000

CMD ["node", "server.js"]
