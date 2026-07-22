"use client";

import { memo } from "react";
import type { ReaderItem } from "../types";

type SeparatorItem = Extract<ReaderItem, { kind: "chapter-separator" }>;

function ChapterSeparatorView({ item }: { item: SeparatorItem }) {
  return (
    <div className="chapter-separator" aria-labelledby={item.key}>
      <h2 id={item.key}>
        Chapter {item.chapterNumber}
        {item.title ? `: ${item.title}` : ""}
      </h2>
    </div>
  );
}

export const ChapterSeparator = memo(ChapterSeparatorView);
