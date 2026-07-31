"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuthStore } from "@/stores/auth";
import { api, resolveOptionalMediaUrl } from "@/lib/api";
import type { Series } from "@/lib/types";

// ─── Constants ───────────────────────────────────────────────────────────────

const STATUS_LABELS: Record<string, string> = {
  ongoing: "Ongoing",
  completed: "Completed",
  hiatus: "Hiatus",
  cancelled: "Cancelled",
};

const CONTENT_EMOJI: Record<string, string> = {
  manga: "\u{1F1EF}\u{1F1F5}",
  manhua: "\u{1F1E8}\u{1F1F3}",
  manhwa: "\u{1F1F0}\u{1F1F7}",
};

// ─── Sub-components ──────────────────────────────────────────────────────────

function Header({
  onSearchToggle,
  searchOpen,
}: {
  onSearchToggle: () => void;
  searchOpen: boolean;
}) {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const router = useRouter();

  const handleLogout = async () => {
    await logout();
    router.push("/");
  };

  return (
    <header className="header">
      <div className="header-inner">
        <Link href="/" className="brand">
          <span className="brand-title">∞ InfinityScan</span>
          <span className="brand-sub">Manga · Manhwa · Manhua reader</span>
        </Link>
        <div style={{ flex: 1 }} />
        <button
          className={`btn secondary small${searchOpen ? " active" : ""}`}
          onClick={onSearchToggle}
          aria-label="Toggle search"
        >
          Browse
        </button>
        {user ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginLeft: 8,
            }}
          >
            {user.role === "admin" && (
              <a
                href="/admin"
                className="btn ghost small"
                style={{ fontSize: "0.75rem" }}
              >
                Admin
              </a>
            )}
            <span style={{ fontSize: "0.875rem", color: "var(--text2)" }}>
              {user.username}
            </span>
            <button
              className="btn ghost small"
              onClick={handleLogout}
              aria-label="Logout"
            >
              Logout
            </button>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 6, marginLeft: 8 }}>
            <Link href="/login" className="btn ghost small">
              Login
            </Link>
            <Link href="/register" className="btn small">
              Register
            </Link>
          </div>
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
          placeholder="Search series..."
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          aria-label="Search series"
        />
        {query && (
          <button className="btn ghost small" onClick={() => onQuery("")}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

function SeriesCard({ series }: { series: Series }) {
  const coverUrl = resolveOptionalMediaUrl(series.cover_url);

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

// ─── Page ────────────────────────────────────────────────────────────────────

export default function HomePage() {
  const [allSeries, setAllSeries] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);

  // Fetch series list from the API
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const data = await api.get("/series");
        setAllSeries(
          Array.isArray(data) ? data : ((data as Record<string, unknown>).list ?? (data as Record<string, unknown>).items ?? []) as Series[],
        );
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

  const filtered = query.trim()
    ? allSeries.filter((s) =>
        s.title.toLowerCase().includes(query.toLowerCase()),
      )
    : allSeries;

  return (
    <>
      <Header
        searchOpen={searchOpen}
        onSearchToggle={() => setSearchOpen((v) => !v)}
      />
      <SearchBar open={searchOpen} query={query} onQuery={setQuery} />

      <main className="wrap">
        {/* Status summary */}
        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            marginBottom: 4,
          }}
        >
          <span className="pill">
            {loading ? "Loading..." : `${filtered.length} series`}
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
            API error: {error}
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
            <h2
              style={{
                fontSize: "1.5rem",
                fontWeight: 700,
                marginBottom: 16,
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
            >
              <span style={{ fontSize: "1.25rem" }}>🔥</span> Most Popular
            </h2>
            <div
              className="grid-cards"
              style={{
                gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
              }}
            >
              {filtered.slice(0, 4).map((s) => (
                <SeriesCard key={s.id} series={s} />
              ))}
            </div>
          </section>
        )}

        {/* All Series Section */}
        {!loading && filtered.length > 0 && (
          <section>
            <h2
              style={{
                fontSize: "1.25rem",
                fontWeight: 600,
                marginBottom: 16,
                marginTop: query ? 0 : 16,
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
            >
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
          0%,
          100% {
            opacity: 0.35;
          }
          50% {
            opacity: 0.6;
          }
        }
      `}</style>
    </>
  );
}
