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
  number: number;
  title: string | null;
  page_count: number;
  published_at: string | null;
  pages: PageMeta[];
  prev_chapter_uuid: string | null;
  next_chapter_uuid: string | null;
  chapter_number: number;
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
