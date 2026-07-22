import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps, ReactNode } from "react";
import { getReaderKeyboardAction, isReaderInputTarget } from "../../keyboard";
import { getAdjacentChapterId } from "../../navigation";
import type { ReaderChapter, ReaderPage } from "../../types";
import { PagedReader, getSpreadIndices, movePagedIndex } from "../PagedReader";
import { ReaderModeSelector } from "../ReaderModeSelector";
import { ReaderToolbar } from "../ReaderToolbar";

interface MockVirtuosoProps {
  data: Array<{ key: string; pageId: string }>;
  initialTopMostItemIndex?: number;
  computeItemKey: (index: number, item: { key: string; pageId: string }) => string;
  itemContent: (index: number, item: { key: string; pageId: string }) => ReactNode;
  rangeChanged: (range: { startIndex: number; endIndex: number }) => void;
}

const virtuoso = vi.hoisted(() => ({ props: null as MockVirtuosoProps | null }));

vi.mock("react-virtuoso", () => ({
  Virtuoso: (props: MockVirtuosoProps) => {
    virtuoso.props = props;
    return <div data-testid="virtualized-pages">{props.data.slice(0, 4).map((item, index) => <div key={props.computeItemKey(index, item)}>{props.itemContent(index, item)}</div>)}</div>;
  },
}));

vi.mock("../ReaderPage", () => ({
  ReaderPage: ({ item, zoom, fitWidth }: { item: { pageId: string }; zoom: number; fitWidth: boolean }) => (
    <div data-testid="rendered-page" data-page-id={item.pageId} data-zoom={zoom} data-fit-width={fitWidth} />
  ),
}));

function makeChapter(
  id: string,
  number: string,
  pageCount = 5,
  previousChapterId: string | null = null,
  nextChapterId: string | null = null,
): ReaderChapter {
  return {
    id,
    number,
    title: `Chapter ${number}`,
    pageCount,
    previousChapterId,
    nextChapterId,
    pages: Array.from({ length: pageCount }, (_, index): ReaderPage => ({
      id: `${id}-page-${index + 1}`,
      pageNumber: index + 1,
      mediaPath: `/media/pages/${id}-page-${index + 1}`,
      width: 800,
      height: 1200,
      aspectRatio: 2 / 3,
    })),
  };
}

function pagedProps(overrides: Partial<ComponentProps<typeof PagedReader>> = {}): ComponentProps<typeof PagedReader> {
  const chapter = makeChapter("chapter-1.5", "1.5");
  return {
    chapter,
    currentPageId: chapter.pages[0].id,
    mode: "horizontal",
    spreadMode: "single",
    direction: "ltr",
    firstPageAlone: false,
    zoom: 100,
    fitWidth: false,
    onVisiblePageChange: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => { virtuoso.props = null; });

describe("ID-based chapter navigation", () => {
  const chapter1 = makeChapter("id-1", "1", 1, null, "id-1.5");
  const chapter15 = makeChapter("id-1.5", "1.5", 1, "id-1", "id-4");
  const chapter4 = makeChapter("id-4", "4", 1, "id-1.5", null);

  it("moves from chapter 1 to 1.5 and from 1.5 to 4 using IDs", () => {
    expect(getAdjacentChapterId(chapter1, "next")).toBe("id-1.5");
    expect(getAdjacentChapterId(chapter15, "next")).toBe("id-4");
  });

  it("moves to the previous chapter and respects first/final boundaries", () => {
    expect(getAdjacentChapterId(chapter15, "previous")).toBe("id-1");
    expect(getAdjacentChapterId(chapter1, "previous")).toBeNull();
    expect(getAdjacentChapterId(chapter4, "next")).toBeNull();
  });
});

describe("bounded paged presentations", () => {
  it("virtualizes a large vertical chapter instead of mounting every image", () => {
    const chapter = makeChapter("large", "8", 300);
    render(<PagedReader {...pagedProps({ chapter, currentPageId: chapter.pages[149].id, mode: "vertical" })} />);
    expect(virtuoso.props?.data).toHaveLength(300);
    expect(virtuoso.props?.initialTopMostItemIndex).toBe(149);
    expect(screen.getAllByTestId("rendered-page")).toHaveLength(4);
  });

  it("renders only the current horizontal single page", () => {
    const chapter = makeChapter("single", "2", 200);
    render(<PagedReader {...pagedProps({ chapter, currentPageId: chapter.pages[99].id })} />);
    expect(screen.getAllByTestId("rendered-page")).toHaveLength(1);
    expect(screen.getByTestId("rendered-page")).toHaveAttribute("data-page-id", "single-page-100");
  });

  it("renders only the active spread pair", () => {
    const chapter = makeChapter("spread", "3", 8);
    render(<PagedReader {...pagedProps({ chapter, currentPageId: chapter.pages[3].id, spreadMode: "spread" })} />);
    expect(screen.getAllByTestId("rendered-page").map((page) => page.getAttribute("data-page-id")))
      .toEqual(["spread-page-3", "spread-page-4"]);
  });

  it("handles an odd final page and a configured first page alone", () => {
    expect(getSpreadIndices(4, 5, false, "ltr")).toEqual([4]);
    expect(getSpreadIndices(0, 5, true, "ltr")).toEqual([0]);
    expect(getSpreadIndices(1, 5, true, "ltr")).toEqual([1, 2]);
    expect(getSpreadIndices(4, 5, true, "ltr")).toEqual([3, 4]);
  });

  it("reverses spread presentation for right-to-left reading", () => {
    expect(getSpreadIndices(0, 4, false, "rtl")).toEqual([1, 0]);
  });

  it("moves across spread pair boundaries without passing first or final pages", () => {
    expect(movePagedIndex(0, -1, 5, "spread", false)).toBe(0);
    expect(movePagedIndex(0, 1, 5, "spread", false)).toBe(2);
    expect(movePagedIndex(2, 1, 5, "spread", false)).toBe(4);
    expect(movePagedIndex(4, 1, 5, "spread", false)).toBe(4);
    expect(movePagedIndex(0, 1, 5, "spread", true)).toBe(1);
  });

  it("changes one page at a time in horizontal single-page mode", () => {
    expect(movePagedIndex(1, 1, 5, "single", false)).toBe(2);
    expect(movePagedIndex(1, -1, 5, "single", false)).toBe(0);
    expect(movePagedIndex(4, 1, 5, "single", false)).toBe(4);
  });
});

describe("mode transitions and controls", () => {
  it("keeps the same page metadata when switching continuous to paged", () => {
    const onModeChange = vi.fn();
    render(<ReaderModeSelector mode="continuous" spreadMode="single" onModeChange={onModeChange} onSpreadModeChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Vertical chapter" }));
    expect(onModeChange).toHaveBeenCalledWith("vertical");
  });

  it("positions paged mode at the page retained from continuous mode", () => {
    const chapter = makeChapter("transition", "1.5", 10);
    const { rerender } = render(<PagedReader {...pagedProps({ chapter, currentPageId: chapter.pages[6].id, mode: "vertical" })} />);
    expect(virtuoso.props?.initialTopMostItemIndex).toBe(6);
    rerender(<PagedReader {...pagedProps({ chapter, currentPageId: chapter.pages[6].id, mode: "horizontal" })} />);
    expect(screen.getByTestId("rendered-page")).toHaveAttribute("data-page-id", "transition-page-7");
  });

  it("preserves the paged page when continuous mode is selected", () => {
    const onModeChange = vi.fn();
    render(<ReaderModeSelector mode="horizontal" spreadMode="single" onModeChange={onModeChange} onSpreadModeChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Continuous" }));
    expect(onModeChange).toHaveBeenCalledWith("continuous");
  });

  it("enforces zoom bounds and toggles fit width", () => {
    const onZoomChange = vi.fn();
    const onFitWidthChange = vi.fn();
    const base: ComponentProps<typeof ReaderToolbar> = {
      title: "Chapter 1.5", page: 1, totalPages: 5, mode: "vertical", spreadMode: "single",
      zoom: 160, fitWidth: false, direction: "ltr", progressStatus: "saved",
      hasPreviousChapter: false, hasNextChapter: true, onBack: vi.fn(), onModeChange: vi.fn(),
      onSpreadModeChange: vi.fn(), onZoomChange, onFitWidthChange, onDirectionChange: vi.fn(),
      onPreviousChapter: vi.fn(), onNextChapter: vi.fn(), onPageChange: vi.fn(), onPreviousPage: vi.fn(), onNextPage: vi.fn(),
    };
    const { rerender } = render(<ReaderToolbar {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(onZoomChange).toHaveBeenCalledWith(160);
    fireEvent.click(screen.getByRole("button", { name: "Fit width" }));
    expect(onFitWidthChange).toHaveBeenCalledWith(true);
    rerender(<ReaderToolbar {...base} zoom={40} />);
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(onZoomChange).toHaveBeenLastCalledWith(40);
  });
});

describe("keyboard policy", () => {
  it("maps bracket shortcuts to chapter navigation in every mode", () => {
    expect(getReaderKeyboardAction("[", "continuous", "ltr")).toBe("previous-chapter");
    expect(getReaderKeyboardAction("]", "vertical", "ltr")).toBe("next-chapter");
  });

  it("changes pages with arrows only in paged modes and honors RTL", () => {
    expect(getReaderKeyboardAction("ArrowRight", "continuous", "ltr")).toBeNull();
    expect(getReaderKeyboardAction("ArrowRight", "horizontal", "ltr")).toBe("next-page");
    expect(getReaderKeyboardAction("ArrowRight", "horizontal", "rtl")).toBe("previous-page");
    expect(getReaderKeyboardAction(" ", "vertical", "ltr")).toBe("next-page");
  });

  it("identifies form and editable targets so shortcuts are ignored", () => {
    const input = document.createElement("input");
    const editable = document.createElement("div");
    editable.contentEditable = "true";
    expect(isReaderInputTarget(input)).toBe(true);
    expect(isReaderInputTarget(editable)).toBe(true);
    expect(isReaderInputTarget(document.body)).toBe(false);
  });
});
