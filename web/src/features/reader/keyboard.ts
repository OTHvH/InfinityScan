import type { ReadingDirection, ReadingMode } from "./types";

export type ReaderKeyboardAction =
  | "previous-chapter"
  | "next-chapter"
  | "previous-page"
  | "next-page"
  | "first-page"
  | "last-page"
  | "zoom-in"
  | "zoom-out"
  | "reset-view"
  | "toggle-fit"
  | "toggle-help";

export function isReaderInputTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const editableContainer = target.closest("[contenteditable]");
  const editable = editableContainer?.getAttribute("contenteditable") !== "false";
  return target.isContentEditable
    || target.contentEditable === "true"
    || (!!editableContainer && editable)
    || ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(target.tagName);
}

export function getReaderKeyboardAction(
  key: string,
  mode: ReadingMode,
  direction: ReadingDirection,
): ReaderKeyboardAction | null {
  if (key === "[") return "previous-chapter";
  if (key === "]") return "next-chapter";
  if (key === "?") return "toggle-help";
  if (key === "+" || key === "=") return "zoom-in";
  if (key === "-") return "zoom-out";
  if (key === "0") return "reset-view";
  if (key.toLowerCase() === "f") return "toggle-fit";
  if (mode === "continuous") return null;
  if (key === "ArrowLeft") return direction === "rtl" ? "next-page" : "previous-page";
  if (key === "ArrowRight") return direction === "rtl" ? "previous-page" : "next-page";
  if (key === " ") return "next-page";
  if (key === "Home") return "first-page";
  if (key === "End") return "last-page";
  return null;
}
