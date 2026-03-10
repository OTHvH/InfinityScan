"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

// ─── Types ────────────────────────────────────────────────────────────────────

interface Series {
  id: string;
  slug: string;
  title: string;
  synopsis: string | null;
  cover_url: string | null;
  cover_object_key: string | null;
  content_type: "manga" | "manhua" | "manhwa";
  status: "ongoing" | "completed" | "hiatus" | "cancelled";
  year: number | null;
  is_nsfw: boolean;
}

interface Chapter {
  id: string;
  number: number;
  title: string | null;
  page_count: number;
  published_at: string | null;
}

interface SeriesDetail extends Series {
  chapters: Chapter[];
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

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function SeriesPage(props: { params: Promise<{ slug: string }> }) {
  const params = use(props.params);
  const router = useRouter();
  const [series, setSeries] = useState<SeriesDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        // Try local library first, then fall back to CopyManga
        let res = await fetch(`${API}/library/${params.slug}`, {
          signal: controller.signal,
        });
        if (res.ok) {
          const data = await res.json();
          setSeries(data);
        } else {
          // Fall back to CopyManga API
          res = await fetch(`${API}/series/${params.slug}`, {
            signal: controller.signal,
          });
          if (!res.ok) throw new Error(`API responded with ${res.status}`);
          const data = await res.json();
          // Transform CopyManga response to our format
          setSeries({
            id: data.path_word,
            slug: data.path_word,
            title: data.name,
            synopsis: data.brief,
            cover_url: data.cover,
            cover_object_key: null,
            content_type: "manhua", // Default, CopyManga doesn't specify
            status: data.status || "ongoing",
            year: null,
            is_nsfw: false,
            chapters: [],
          });
        }
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setError(err.message);
        }
      } finally {
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [params.slug]);

  if (loading) {
    return (
      <div className="wrap" style={{ padding: 40, textAlign: "center" }}>
        <div className="spinner" style={{ margin: "0 auto" }} />
        <p style={{ marginTop: 16 }}>Loading...</p>
      </div>
    );
  }

  if (error || !series) {
    return (
      <div className="wrap" style={{ padding: 40, textAlign: "center" }}>
        <p style={{ color: "var(--accent3)" }}>⚠ Error: {error || "Series not found"}</p>
        <button
          className="btn"
          onClick={() => router.push("/")}
          style={{ marginTop: 16 }}
        >
          Go Home
        </button>
      </div>
    );
  }

  const coverUrl =
    series.cover_url ??
    (series.cover_object_key
      ? `${API}/covers/${series.cover_object_key.split("/").map(encodeURIComponent).join("/")}`
      : null);

  // Sort chapters by number descending (newest first)
  const sortedChapters = [...(series.chapters || [])].sort(
    (a, b) => b.number - a.number
  );

  return (
    <>
      <header className="header">
        <div className="header-inner">
          <button
            className="btn ghost"
            onClick={() => router.push("/")}
            aria-label="Go back"
          >
            ← Back
          </button>
          <div style={{ flex: 1 }} />
        </div>
      </header>

      <main className="wrap">
        <div
          style={{
            display: "flex",
            gap: 24,
            marginBottom: 32,
            flexWrap: "wrap",
          }}
        >
          {/* Cover */}
          <div
            style={{
              width: 180,
              minWidth: 180,
              aspectRatio: "2/3",
              borderRadius: 12,
              overflow: "hidden",
              background: "var(--bg2)",
              border: "1px solid var(--border)",
            }}
          >
            {coverUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={coverUrl}
                alt={series.title}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            ) : (
              <div
                style={{
                  width: "100%",
                  height: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 48,
                }}
              >
                {CONTENT_EMOJI[series.content_type] ?? "📖"}
              </div>
            )}
          </div>

          {/* Info */}
          <div style={{ flex: 1, minWidth: 280 }}>
            <h1 style={{ fontSize: "1.75rem", fontWeight: 700, marginBottom: 8 }}>
              {series.title}
            </h1>
            <p style={{ color: "var(--text2)", marginBottom: 12 }}>
              {CONTENT_EMOJI[series.content_type]} {series.content_type} ·{" "}
              <span className={`badge badge-${series.status}`}>
                {STATUS_LABELS[series.status]}
              </span>
              {series.year && ` · ${series.year}`}
            </p>
            {series.synopsis && (
              <p style={{ color: "var(--text2)", lineHeight: 1.6 }}>
                {series.synopsis}
              </p>
            )}
          </div>
        </div>

        {/* Chapters */}
        <section>
          <h2 style={{ fontSize: "1.25rem", fontWeight: 600, marginBottom: 16 }}>
            Chapters ({sortedChapters.length})
          </h2>
          {sortedChapters.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>No chapters available.</p>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
                gap: 12,
              }}
            >
              {sortedChapters.map((chapter) => (
                <a
                  key={chapter.id}
                  href={`/reader/${series.slug}/${chapter.number}`}
                  style={{
                    display: "block",
                    padding: 16,
                    background: "var(--bg2)",
                    border: "1px solid var(--border)",
                    borderRadius: 8,
                    textDecoration: "none",
                    transition: "border-color 0.2s",
                  }}
                >
                  <div style={{ fontWeight: 600 }}>
                    {chapter.title || `Chapter ${chapter.number}`}
                  </div>
                  <div
                    style={{
                      fontSize: "0.875rem",
                      color: "var(--muted)",
                      marginTop: 4,
                    }}
                  >
                    {chapter.page_count} pages
                  </div>
                </a>
              ))}
            </div>
          )}
        </section>
      </main>
    </>
  );
}
