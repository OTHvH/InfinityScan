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
};

export default nextConfig;
