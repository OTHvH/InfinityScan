"use client";

interface ReaderErrorFooterProps {
  error: string | null;
  onRetry: () => void;
}

export function ReaderErrorFooter({ error, onRetry }: ReaderErrorFooterProps) {
  if (!error) return null;
  return (
    <div role="alert" aria-live="assertive" className="reader-error-footer">
      <span>Could not load more chapters: {error}</span>
      <button className="btn small" type="button" onClick={onRetry}>Retry</button>
    </div>
  );
}
