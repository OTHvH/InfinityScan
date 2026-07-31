"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, resolveOptionalMediaUrl } from "@/lib/api";
import { compareDecimalStrings, type Series, type Chapter } from "@/lib/types";

// ─── Helpers ─────────────────────────────────────────────────────────────────

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

// ─── Types ───────────────────────────────────────────────────────────────────

interface SeriesDetail extends Series {
  chapters: Chapter[];
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function SeriesPage(props: {
  params: Promise<{ slug: string }>;
}) {
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
        try {
          const data = await api.get(`/library/${params.slug}`);
          setSeries(data as SeriesDetail);
        } catch {
          const data = await api.get(`/series/${params.slug}`);
          const d = data as Record<string, unknown>;
          setSeries({
            id: (d.path_word as string) ?? params.slug,
            slug: (d.path_word as string) ?? params.slug,
            title: (d.name as string) ?? "",
            synopsis: (d.brief as string) ?? null,
            cover_url: (d.cover_url as string) ?? null,
            content_type: "manhua",
            status: ((d.status as string) || "ongoing") as SeriesDetail["status"],
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
        <p style={{ color: "var(--accent3)" }}>
          Error: {error || "Series not found"}
        </p>
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

  const coverUrl = resolveOptionalMediaUrl(series.cover_url);

  // Sort chapters by number descending (newest first)
  const sortedChapters = [...(series.chapters || [])].sort(
    (a, b) => compareDecimalStrings(b.number, a.number),
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
            <h1
              style={{ fontSize: "1.75rem", fontWeight: 700, marginBottom: 8 }}
            >
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
          <h2
            style={{ fontSize: "1.25rem", fontWeight: 600, marginBottom: 16 }}
          >
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
                  href={`/reader/${series.slug}/${chapter.id}`}
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
