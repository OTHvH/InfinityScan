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

// ─── Helpers ──────────────────────────────────────────────────────────────────

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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
}: {
  onSearchToggle: () => void;
  searchOpen: boolean;
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

// ─── Page ─────────────────────────────────────────────────────────────────────

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
      />
      <SearchBar open={searchOpen} query={query} onQuery={setQuery} />

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
        {!loading && filtered.length > 0 && (
          <div className="grid-cards">
            {filtered.map((s) => (
              <SeriesCard key={s.id} series={s} />
            ))}
          </div>
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
