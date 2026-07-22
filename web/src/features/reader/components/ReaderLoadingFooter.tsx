"use client";

export function ReaderLoadingFooter({ loading }: { loading: boolean }) {
  if (!loading) return null;
  return <div role="status" aria-live="polite" className="reader-loading-footer">Loading more chapters...</div>;
}
