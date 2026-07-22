export interface User {
  id: string;
  username: string;
  email: string | null;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface Series {
  id: string;
  slug: string;
  title: string;
  synopsis: string | null;
  cover_url?: string | null;
  cover_object_key: string | null;
  content_type: "manga" | "manhua" | "manhwa";
  status: "ongoing" | "completed" | "hiatus" | "cancelled";
  year: number | null;
  is_nsfw: boolean;
}

export interface Chapter {
  id: string;
  uuid: string;
  name: string;
  number: string;
  title: string | null;
  page_count: number;
  published_at: string | null;
  pages: PageMeta[];
  prev_chapter_uuid: string | null;
  next_chapter_uuid: string | null;
  chapter_number: string;
}

export interface PageMeta {
  page_number: number;
  url: string;
}

export interface Bookmark {
  id: string;
  series_path_word: string;
  series_name: string;
}

export interface ReadingProgress {
  chapter_uuid: string;
  last_page: number | null;
  scroll_position: number | null;
  completed: boolean;
}

/** Compare chapter numbers without converting Decimal-compatible strings to floats. */
export function compareDecimalStrings(left: string, right: string): number {
  const normalize = (value: string) => {
    const [integerPart = "0", fractionPart = ""] = value.trim().split(".", 2);
    const negative = integerPart.startsWith("-");
    const integer = (negative ? integerPart.slice(1) : integerPart).replace(/^0+(?=\d)/, "") || "0";
    const fraction = fractionPart.replace(/0+$/, "");
    return { negative, integer, fraction };
  };

  const a = normalize(left);
  const b = normalize(right);
  if (a.negative !== b.negative) return a.negative ? -1 : 1;
  const sign = a.negative ? -1 : 1;
  if (a.integer.length !== b.integer.length) {
    return sign * (a.integer.length < b.integer.length ? -1 : 1);
  }
  if (a.integer !== b.integer) return sign * (a.integer < b.integer ? -1 : 1);
  const width = Math.max(a.fraction.length, b.fraction.length);
  const af = a.fraction.padEnd(width, "0");
  const bf = b.fraction.padEnd(width, "0");
  if (af === bf) return 0;
  return sign * (af < bf ? -1 : 1);
}
