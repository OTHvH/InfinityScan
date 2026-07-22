import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentType, ReactNode } from "react";
import { ChapterSeparator } from "../ChapterSeparator";
import { ContinuousReader } from "../ContinuousReader";
import { ReaderPage } from "../ReaderPage";
import type { ReaderItem } from "../../types";

interface MockVirtuosoProps {
  data: ReaderItem[];
  components?: { Footer?: ComponentType };
  computeItemKey: (index: number, item: ReaderItem) => string;
  itemContent: (index: number, item: ReaderItem) => ReactNode;
  endReached: () => void;
  startReached: () => void;
}

const virtuoso = vi.hoisted(() => ({ props: null as MockVirtuosoProps | null }));

vi.mock("react-virtuoso", () => ({
  Virtuoso: (props: MockVirtuosoProps) => {
    virtuoso.props = props;
    const Footer = props.components?.Footer;
    return (
      <div data-testid="virtual-list">
        {props.data.slice(0, 2).map((item: ReaderItem, index: number) => (
          <div data-testid="virtual-item" key={props.computeItemKey(index, item)}>{props.itemContent(index, item)}</div>
        ))}
        {Footer && <Footer />}
      </div>
    );
  },
}));

function pageItem(id: string): Extract<ReaderItem, { kind: "page" }> {
  return {
    kind: "page",
    key: `page:${id}`,
    chapterId: "chapter-1",
    chapterNumber: "1.5",
    pageId: id,
    pageNumber: 1,
    mediaPath: `/media/pages/${id}`,
    width: 100,
    height: 150,
    aspectRatio: 2 / 3,
  };
}

const separator: Extract<ReaderItem, { kind: "chapter-separator" }> = {
  kind: "chapter-separator",
  key: "chapter:chapter-1",
  chapterId: "chapter-1",
  chapterNumber: "1.5",
  title: "Bonus",
};

function props(overrides: Partial<React.ComponentProps<typeof ContinuousReader>> = {}) {
  return {
    items: [separator, ...Array.from({ length: 100 }, (_, index) => pageItem(`page-${index}`))],
    firstItemIndex: 1000,
    hasMoreNext: true,
    hasMorePrevious: true,
    loadingNext: false,
    loadingPrevious: false,
    error: null,
    zoom: 100,
    fitWidth: true,
    onLoadNext: vi.fn(() => Promise.resolve()),
    onLoadPrevious: vi.fn(() => Promise.resolve()),
    onRetry: vi.fn(() => Promise.resolve()),
    onRangeChanged: vi.fn(),
    ...overrides,
  };
}

describe("ContinuousReader", () => {
  beforeEach(() => {
    virtuoso.props = null;
  });

  it("renders a bounded virtual range with stable keys and chapter separators", () => {
    render(<ContinuousReader {...props()} />);

    expect(screen.getAllByTestId("virtual-item")).toHaveLength(2);
    expect(virtuoso.props?.computeItemKey(0, separator)).toBe("chapter:chapter-1");
    expect(screen.getByRole("heading", { name: "Chapter 1.5: Bonus" })).toBeInTheDocument();
  });

  it("uses endReached and the footer observer as one deduplicated next trigger", async () => {
    let resolve!: () => void;
    const onLoadNext = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    let observerCallback!: IntersectionObserverCallback;
    class Observer {
      constructor(callback: IntersectionObserverCallback) { observerCallback = callback; }
      observe() {}
      disconnect() {}
      unobserve() {}
      takeRecords(): IntersectionObserverEntry[] { return []; }
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    Object.defineProperty(window, "IntersectionObserver", { configurable: true, value: Observer });
    render(<ContinuousReader {...props({ onLoadNext })} />);

    virtuoso.props?.endReached();
    virtuoso.props?.endReached();
    observerCallback([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
    expect(onLoadNext).toHaveBeenCalledTimes(1);
    resolve();
  });

  it("does not request at the final cursor and supports previous loading", () => {
    const onLoadNext = vi.fn(() => Promise.resolve());
    const onLoadPrevious = vi.fn(() => Promise.resolve());
    render(<ContinuousReader {...props({ hasMoreNext: false, onLoadNext, onLoadPrevious })} />);

    virtuoso.props?.endReached();
    virtuoso.props?.startReached();
    expect(onLoadNext).not.toHaveBeenCalled();
    expect(onLoadPrevious).toHaveBeenCalledTimes(1);
  });

  it("renders an accessible retry footer", () => {
    const onRetry = vi.fn(() => Promise.resolve());
    render(<ContinuousReader {...props({ error: "Network failed", onRetry })} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Network failed");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("reader item components", () => {
  it("uses known dimensions for an aspect-ratio placeholder and accessible alt text", () => {
    render(<ReaderPage item={pageItem("page-1")} zoom={100} fitWidth />);
    const image = screen.getByRole("img", { name: "Chapter 1.5, page 1" });
    expect(image).toHaveAttribute("width", "100");
    expect(image).toHaveAttribute("height", "150");
    expect(image.parentElement).toHaveStyle({ aspectRatio: "100 / 150" });
  });

  it("renders chapter separators as headings", () => {
    render(<ChapterSeparator item={separator} />);
    expect(screen.getByRole("heading", { name: "Chapter 1.5: Bonus" })).toBeInTheDocument();
  });
});
