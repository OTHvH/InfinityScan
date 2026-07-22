"use client";

import { memo, useState } from "react";
import type { ReaderItem } from "../types";

type PageItem = Extract<ReaderItem, { kind: "page" }>;

interface ReaderPageProps {
  item: PageItem;
  zoom: number;
  fitWidth: boolean;
  seeking?: boolean;
}

function ReaderPageView({ item, zoom, fitWidth, seeking = false }: ReaderPageProps) {
  const [failed, setFailed] = useState(false);
  const aspectRatio = item.width > 0 && item.height > 0
    ? `${item.width} / ${item.height}`
    : `${item.aspectRatio || 2 / 3}`;

  if (failed) {
    return (
      <div className="page-error" role="img" aria-label={`Failed to load Chapter ${item.chapterNumber}, page ${item.pageNumber}`}>
        Failed to load Chapter {item.chapterNumber}, page {item.pageNumber}
      </div>
    );
  }

  if (seeking) {
    return (
      <div
        className="page page-placeholder"
        style={{ aspectRatio, width: fitWidth ? "100%" : `${zoom}%` }}
        aria-label={`Loading Chapter ${item.chapterNumber}, page ${item.pageNumber}`}
      />
    );
  }

  return (
    <div className="page" style={{ aspectRatio }}>
      <img
        src={item.mediaPath}
        alt={`Chapter ${item.chapterNumber}, page ${item.pageNumber}`}
        width={item.width || undefined}
        height={item.height || undefined}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
        style={{
          transform: fitWidth ? undefined : `scale(${zoom / 100})`,
          transformOrigin: "top center",
          maxWidth: fitWidth ? "100%" : `${zoom}%`,
          width: fitWidth ? "100%" : "auto",
          height: "auto",
        }}
      />
    </div>
  );
}

export const ReaderPage = memo(ReaderPageView);
