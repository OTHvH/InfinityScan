"use client";

import { Suspense, useState, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useAuthStore } from "@/stores/auth";
import { api, getReturnUrl, ApiError } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const login = useAuthStore((s) => s.login);
  const user = useAuthStore((s) => s.user);

  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const returnUrl = getReturnUrl(searchParams);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setError("");
      setLoading(true);

      try {
        await api.fetchCsrf();
        await login({ username: identifier, password });
        router.push(returnUrl);
      } catch (err) {
        if (err instanceof ApiError) {
          if (err.status === 401) {
            setError("Invalid username or password");
          } else {
            setError(err.detail || "Login failed");
          }
        } else {
          setError("An unexpected error occurred");
        }
      } finally {
        setLoading(false);
      }
    },
    [identifier, password, login, router, returnUrl],
  );

  if (user) {
    router.replace(returnUrl);
    return null;
  }

  return (
    <main
      className="wrap"
      style={{ display: "flex", justifyContent: "center", paddingTop: 60 }}
    >
      <div className="modal" style={{ maxWidth: 420, width: "100%" }}>
        <h2 style={{ marginBottom: 20, textAlign: "center" }}>Login</h2>

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 14 }}>
            <label
              htmlFor="login-identifier"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Username
            </label>
            <input
              id="login-identifier"
              type="text"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              className="search-input"
              style={{ width: "100%" }}
              required
              autoFocus
              autoComplete="username"
              minLength={1}
            />
          </div>

          <div style={{ marginBottom: 18 }}>
            <label
              htmlFor="login-password"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Password
            </label>
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="search-input"
              style={{ width: "100%" }}
              required
              minLength={8}
              autoComplete="current-password"
            />
          </div>

          {error && (
            <p
              style={{
                color: "var(--accent3)",
                marginBottom: 14,
                fontSize: "0.875rem",
              }}
            >
              {error}
            </p>
          )}

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <Link
              href="/register"
              style={{
                fontSize: "0.875rem",
                color: "var(--accent2)",
              }}
            >
              Create account
            </Link>
            <button type="submit" className="btn" disabled={loading}>
              {loading ? "Logging in..." : "Login"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <>
      <header className="header">
        <div className="header-inner">
          <Link href="/" className="brand">
            <span className="brand-title">∞ InfinityScan</span>
          </Link>
        </div>
      </header>
      <Suspense
        fallback={
          <main
            className="wrap"
            style={{ display: "flex", justifyContent: "center", paddingTop: 60 }}
          >
            <div className="spinner" style={{ margin: "0 auto" }} />
          </main>
        }
      >
        <LoginForm />
      </Suspense>
    </>
  );
}
