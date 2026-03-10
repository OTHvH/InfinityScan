"use client";

import { useEffect, useRef, useState } from "react";

// ─── Types ────────────────────────────────────────────────────────────────────

interface Series {
  id: string;
  slug: string;
  title: string;
  synopsis: string | null;
  /** Resolved CDN/S3 URL returned by the local-library listing endpoint. */
  cover_url?: string | null;
  /** S3 object key — present on local-library rows when cover_url is absent. */
  cover_object_key: string | null;
  content_type: "manga" | "manhua" | "manhwa";
  status: "ongoing" | "completed" | "hiatus" | "cancelled";
  year: number | null;
  is_nsfw: boolean;
}

interface User {
  id: string;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
}

interface Token {
  access_token: string;
  token_type: string;
  user: User;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  const headers: HeadersInit = { "Content-Type": "application/json" };
  const token = localStorage.getItem("token");
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

const STATUS_LABELS: Record<string, string> = {
  ongoing: "Ongoing",
  completed: "Completed",
  hiatus: "Hiatus",
  cancelled: "Cancelled",
};

const CONTENT_EMOJI: Record<string, string> = {
  manga: "🇯🇵",
  manhua: "🇨🇳",
  manhwa: "🇰🇷",
};

// ─── Sub-components ───────────────────────────────────────────────────────────

function Header({
  onSearchToggle,
  searchOpen,
  user,
  onLoginClick,
  onLogout,
}: {
  onSearchToggle: () => void;
  searchOpen: boolean;
  user: User | null;
  onLoginClick: () => void;
  onLogout: () => void;
}) {
  return (
    <header className="header">
      <div className="header-inner">
        <div className="brand">
          <span className="brand-title">∞ InfinityScan</span>
          <span className="brand-sub">Manga · Manhwa · Manhua reader</span>
        </div>
        <div style={{ flex: 1 }} />
        <button
          className={`btn secondary small${searchOpen ? " active" : ""}`}
          onClick={onSearchToggle}
          aria-label="Toggle search"
        >
          🔍 Browse
        </button>
        {user ? (
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: 8 }}>
            <span style={{ fontSize: "0.875rem", color: "var(--text2)" }}>
              👤 {user.username}
            </span>
            <button className="btn ghost small" onClick={onLogout} aria-label="Logout">
              Logout
            </button>
          </div>
        ) : (
          <button className="btn small" onClick={onLoginClick} style={{ marginLeft: 8 }}>
            Login
          </button>
        )}
      </div>
    </header>
  );
}

function SearchBar({
  open,
  query,
  onQuery,
}: {
  open: boolean;
  query: string;
  onQuery: (q: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;
  return (
    <div className="wrap" style={{ paddingTop: 10, paddingBottom: 10 }}>
      <div className="search-row">
        <input
          ref={inputRef}
          className="search-input"
          type="search"
          placeholder="Search series…"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          aria-label="Search series"
        />
        {query && (
          <button className="btn ghost small" onClick={() => onQuery("")}>
            ✕ Clear
          </button>
        )}
      </div>
    </div>
  );
}

function SeriesCard({ series }: { series: Series }) {
  // Prefer the pre-resolved URL from the API; fall back to constructing from the
  // object key (useful when the frontend is pointed at a custom S3/CDN proxy).
  const coverUrl =
    series.cover_url ??
    (series.cover_object_key
      ? `${API}/covers/${series.cover_object_key.split("/").map(encodeURIComponent).join("/")}`
      : null);

  return (
    <div className="card">
      <a href={`/series/${series.slug}`} aria-label={series.title}>
        <div className="card-thumb">
          {coverUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={coverUrl} alt={series.title} loading="lazy" />
          ) : (
            <span className="card-thumb-placeholder">
              {CONTENT_EMOJI[series.content_type] ?? "📖"}
            </span>
          )}
        </div>
      </a>
      <div className="card-info">
        <p className="card-title">{series.title}</p>
        <p className="card-meta">
          <span className={`badge badge-${series.status}`}>
            {STATUS_LABELS[series.status]}
          </span>
          &ensp;
          {CONTENT_EMOJI[series.content_type]} {series.content_type}
          {series.year ? ` · ${series.year}` : ""}
        </p>
      </div>
      <div className="card-actions">
        <a href={`/series/${series.slug}`} className="btn small">
          Read
        </a>
      </div>
    </div>
  );
}

function EmptyState({ searching }: { searching: boolean }) {
  return (
    <div className="empty-state">
      <div className="icon">{searching ? "🔍" : "📚"}</div>
      <p>
        {searching
          ? "No series match your search."
          : "No series found. Import some content to get started."}
      </p>
    </div>
  );
}

function LoginModal({
  open,
  onClose,
  onLogin,
}: {
  open: boolean;
  onClose: () => void;
  onLogin: (token: Token) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const formData = new URLSearchParams();
      formData.append("username", username);
      formData.append("password", password);

      const res = await fetch(`${API}/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Login failed");
      }

      const token: Token = await res.json();
      localStorage.setItem("token", token.access_token);
      localStorage.setItem("user", JSON.stringify(token.user));
      onLogin(token);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2 style={{ marginBottom: 16 }}>Login</h2>
        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: "block", marginBottom: 4, fontSize: "0.875rem" }}>
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="search-input"
              required
              autoFocus
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 4, fontSize: "0.875rem" }}>
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="search-input"
              required
            />
          </div>
          {error && (
            <p style={{ color: "var(--accent3)", marginBottom: 12, fontSize: "0.875rem" }}>
              ⚠ {error}
            </p>
          )}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button type="button" className="btn ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn" disabled={loading}>
              {loading ? "Logging in..." : "Login"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function HomePage() {
  const [allSeries, setAllSeries] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);

  // Load user from localStorage on mount
  useEffect(() => {
    const storedUser = localStorage.getItem("user");
    if (storedUser) {
      try {
        setUser(JSON.parse(storedUser));
      } catch {}
    }
  }, []);

  const handleLogin = (token: Token) => {
    setUser(token.user);
  };

  const handleLogout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    setUser(null);
  };

  // Fetch series list from the API
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(`${API}/series`, { signal: controller.signal });
        if (!res.ok) throw new Error(`API responded with ${res.status}`);
        const data = await res.json();
        setAllSeries(Array.isArray(data) ? data : (data.list ?? data.items ?? []));
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setError(err.message);
        }
      } finally {
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, []);

  // Client-side filter
  const filtered = query.trim()
    ? allSeries.filter((s) =>
        s.title.toLowerCase().includes(query.toLowerCase())
      )
    : allSeries;

  return (
    <>
      <Header
        searchOpen={searchOpen}
        onSearchToggle={() => setSearchOpen((v) => !v)}
        user={user}
        onLoginClick={() => setLoginOpen(true)}
        onLogout={handleLogout}
      />
      <SearchBar open={searchOpen} query={query} onQuery={setQuery} />
      <LoginModal
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        onLogin={handleLogin}
      />

      <main className="wrap">
        {/* Status summary */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
          <span className="pill">
            {loading ? "Loading…" : `${filtered.length} series`}
          </span>
          {query && (
            <span className="pill" style={{ color: "var(--accent2)" }}>
              Filtered: &ldquo;{query}&rdquo;
            </span>
          )}
        </div>

        {/* Error */}
        {error && (
          <p
            style={{
              color: "var(--accent3)",
              fontFamily: "var(--font-mono)",
              fontSize: 13,
              marginTop: 12,
              padding: "10px 14px",
              borderRadius: 14,
              border: "1px solid rgba(255,77,246,.22)",
              background: "rgba(255,77,246,.06)",
            }}
          >
            ⚠ API error: {error}
          </p>
        )}

        {/* Loading skeleton */}
        {loading && (
          <div className="grid-cards">
            {Array.from({ length: 12 }).map((_, i) => (
              <div key={i} className="card" style={{ opacity: 0.35 }}>
                <div
                  className="card-thumb"
                  style={{ animation: "pulse 1.8s ease-in-out infinite" }}
                />
                <div className="card-info">
                  <p
                    className="card-title"
                    style={{
                      background: "rgba(184,77,255,.2)",
                      borderRadius: 8,
                      height: 14,
                      width: "70%",
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Results */}
        {!loading && filtered.length === 0 && (
          <EmptyState searching={!!query} />
        )}
        
        {/* Most Popular Section */}
        {!loading && filtered.length > 0 && !query && (
          <section style={{ marginBottom: 32 }}>
            <h2 style={{ 
              fontSize: "1.5rem", 
              fontWeight: 700, 
              marginBottom: 16,
              display: "flex",
              alignItems: "center",
              gap: 8 
            }}>
              <span style={{ fontSize: "1.25rem" }}>🔥</span> Most Popular
            </h2>
            <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}>
              {filtered.slice(0, 4).map((s) => (
                <SeriesCard key={s.id} series={s} />
              ))}
            </div>
          </section>
        )}

        {/* All Series Section */}
        {!loading && filtered.length > 0 && (
          <section>
            <h2 style={{ 
              fontSize: "1.25rem", 
              fontWeight: 600, 
              marginBottom: 16,
              marginTop: query ? 0 : 16,
              display: "flex",
              alignItems: "center",
              gap: 8 
            }}>
              <span style={{ fontSize: "1rem" }}>📚</span> 
              {query ? `Search Results (${filtered.length})` : "All Series"}
            </h2>
            <div className="grid-cards">
              {(query ? filtered : filtered.slice(4)).map((s) => (
                <SeriesCard key={s.id} series={s} />
              ))}
            </div>
          </section>
        )}
      </main>

      <style jsx global>{`
        @keyframes pulse {
          0%, 100% { opacity: 0.35; }
          50% { opacity: 0.6; }
        }
      `}</style>
    </>
  );
}
