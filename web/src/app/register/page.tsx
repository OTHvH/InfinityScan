"use client";

import { Suspense, useState, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useAuthStore } from "@/stores/auth";
import { getReturnUrl, ApiError } from "@/lib/api";

function RegisterForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const register = useAuthStore((s) => s.register);
  const user = useAuthStore((s) => s.user);

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);

  const returnUrl = getReturnUrl(searchParams);

  const validate = useCallback((): boolean => {
    const errs: Record<string, string> = {};

    if (!/^[a-zA-Z0-9_]+$/.test(username)) {
      errs.username = "Username can only contain letters, numbers, and underscores";
    } else if (username.length < 3) {
      errs.username = "Username must be at least 3 characters";
    } else if (username.length > 50) {
      errs.username = "Username must be at most 50 characters";
    }

    if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      errs.email = "Please enter a valid email address";
    }

    if (password.length < 8) {
      errs.password = "Password must be at least 8 characters";
    } else if (password.length > 128) {
      errs.password = "Password must be at most 128 characters";
    }

    if (password !== confirmPassword) {
      errs.confirmPassword = "Passwords do not match";
    }

    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  }, [username, email, password, confirmPassword]);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setError("");
      setFieldErrors({});

      if (!validate()) return;

      setLoading(true);

      try {
        await register({
          username,
          password,
          ...(email ? { email } : {}),
        });
        router.push(returnUrl);
      } catch (err) {
        if (err instanceof ApiError) {
          if (err.detail.includes("Username already")) {
            setFieldErrors((prev) => ({ ...prev, username: "Username is already taken" }));
          } else if (err.detail.includes("Email already")) {
            setFieldErrors((prev) => ({ ...prev, email: "Email is already registered" }));
          } else {
            setError(err.detail || "Registration failed");
          }
        } else {
          setError("An unexpected error occurred");
        }
      } finally {
        setLoading(false);
      }
    },
    [username, email, password, register, router, returnUrl, validate],
  );

  if (user) {
    router.replace(returnUrl);
    return null;
  }

  const fieldStyle = (field: string): React.CSSProperties => ({
    borderColor: fieldErrors[field] ? "var(--accent3)" : undefined,
  });

  return (
    <main
      className="wrap"
      style={{ display: "flex", justifyContent: "center", paddingTop: 40 }}
    >
      <div className="modal" style={{ maxWidth: 420, width: "100%" }}>
        <h2 style={{ marginBottom: 20, textAlign: "center" }}>
          Create Account
        </h2>

        <form onSubmit={handleSubmit}>
          {/* Username */}
          <div style={{ marginBottom: 14 }}>
            <label
              htmlFor="reg-username"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Username *
            </label>
            <input
              id="reg-username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="search-input"
              style={{ width: "100%", ...fieldStyle("username") }}
              required
              autoFocus
              autoComplete="username"
              minLength={3}
              maxLength={50}
              pattern="^[a-zA-Z0-9_]+$"
            />
            {fieldErrors.username && (
              <p style={{ color: "var(--accent3)", fontSize: "0.75rem", marginTop: 4 }}>
                {fieldErrors.username}
              </p>
            )}
          </div>

          {/* Email */}
          <div style={{ marginBottom: 14 }}>
            <label
              htmlFor="reg-email"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Email (optional)
            </label>
            <input
              id="reg-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="search-input"
              style={{ width: "100%", ...fieldStyle("email") }}
              autoComplete="email"
            />
            {fieldErrors.email && (
              <p style={{ color: "var(--accent3)", fontSize: "0.75rem", marginTop: 4 }}>
                {fieldErrors.email}
              </p>
            )}
          </div>

          {/* Password */}
          <div style={{ marginBottom: 14 }}>
            <label
              htmlFor="reg-password"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Password *
            </label>
            <input
              id="reg-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="search-input"
              style={{ width: "100%", ...fieldStyle("password") }}
              required
              minLength={8}
              maxLength={128}
              autoComplete="new-password"
            />
            {fieldErrors.password && (
              <p style={{ color: "var(--accent3)", fontSize: "0.75rem", marginTop: 4 }}>
                {fieldErrors.password}
              </p>
            )}
          </div>

          {/* Confirm Password */}
          <div style={{ marginBottom: 18 }}>
            <label
              htmlFor="reg-confirm"
              style={{
                display: "block",
                marginBottom: 6,
                fontSize: "0.875rem",
                color: "var(--text2)",
              }}
            >
              Confirm Password *
            </label>
            <input
              id="reg-confirm"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="search-input"
              style={{ width: "100%", ...fieldStyle("confirmPassword") }}
              required
              minLength={8}
              maxLength={128}
              autoComplete="new-password"
            />
            {fieldErrors.confirmPassword && (
              <p style={{ color: "var(--accent3)", fontSize: "0.75rem", marginTop: 4 }}>
                {fieldErrors.confirmPassword}
              </p>
            )}
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
              href="/login"
              style={{
                fontSize: "0.875rem",
                color: "var(--accent2)",
              }}
            >
              Back to login
            </Link>
            <button type="submit" className="btn" disabled={loading}>
              {loading ? "Creating account..." : "Register"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}

export default function RegisterPage() {
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
            style={{
              display: "flex",
              justifyContent: "center",
              paddingTop: 40,
            }}
          >
            <div className="spinner" style={{ margin: "0 auto" }} />
          </main>
        }
      >
        <RegisterForm />
      </Suspense>
    </>
  );
}
