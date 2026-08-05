import type { NextConfig } from "next";

export function resolveApiInternalUrl(
  value: string | undefined,
  environment: string | undefined,
): string {
  if (!value) {
    if (environment === "development") return "http://localhost:8000";
    throw new Error(
      "API_INTERNAL_URL is required outside development and must be an absolute HTTP(S) origin",
    );
  }

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("API_INTERNAL_URL must be an absolute HTTP(S) origin");
  }

  const canonicalOrigin = url.origin;
  if (
    (url.protocol !== "http:" && url.protocol !== "https:")
    || url.username
    || url.password
    || url.pathname !== "/"
    || url.search
    || url.hash
    || (value !== canonicalOrigin && value !== `${canonicalOrigin}/`)
  ) {
    throw new Error(
      "API_INTERNAL_URL must be an absolute HTTP(S) origin without credentials, path, query, or fragment",
    );
  }

  return canonicalOrigin;
}

export function resolveMediaCspOrigins(value: string | undefined): string[] {
  const developmentDefaults = [
    "http://host.docker.internal:*",
    "http://127.0.0.1:*",
    "http://localhost:*",
  ];
  const origins = (value?.trim() || (process.env.APP_ENV === "development" ? developmentDefaults.join(",") : ""))
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
  const localHttpOrigin = /^http:\/\/(?:localhost|127(?:\.\d{1,3}){3}|host\.docker\.internal)(?::\*)?$/;
  for (const origin of origins) {
    if (localHttpOrigin.test(origin)) continue;
    let parsed: URL;
    try {
      parsed = new URL(origin);
    } catch {
      throw new Error("MEDIA_CSP_ORIGINS must contain exact HTTPS origins");
    }
    if (
      parsed.protocol !== "https:"
      || !parsed.hostname
      || parsed.username
      || parsed.password
      || parsed.pathname !== "/"
      || parsed.search
      || parsed.hash
      || parsed.hostname.includes("*")
      || /[\s;'"<>]/.test(origin)
    ) {
      throw new Error("MEDIA_CSP_ORIGINS must contain exact HTTPS origins");
    }
  }
  return origins.map((origin) => origin.endsWith("/") ? origin.slice(0, -1) : origin);
}

const mediaCspOrigins = resolveMediaCspOrigins(process.env.MEDIA_CSP_ORIGINS);
const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self' data:",
  `img-src 'self' data: blob: ${mediaCspOrigins.join(" ")}`,
  "connect-src 'self'",
].join("; ");

const nextConfig: NextConfig = {
  output: "standalone",
  reactCompiler: true,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${resolveApiInternalUrl(process.env.API_INTERNAL_URL, process.env.NODE_ENV)}/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(), payment=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
