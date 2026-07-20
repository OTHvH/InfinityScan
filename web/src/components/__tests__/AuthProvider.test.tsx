import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const loadCurrentUser = vi.fn();

vi.mock("@/stores/auth", () => ({
  useAuthStore: Object.assign(
    (selector: (s: { loadCurrentUser: () => Promise<void> }) => unknown) =>
      selector({ loadCurrentUser }),
    { setState: vi.fn() },
  ),
}));

import AuthProvider from "@/components/AuthProvider";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AuthProvider", () => {
  it("renders children", () => {
    render(
      <AuthProvider>
        <div data-testid="child">Hello</div>
      </AuthProvider>,
    );
    expect(screen.getByTestId("child")).toHaveTextContent("Hello");
  });

  it("calls loadCurrentUser on mount", () => {
    render(
      <AuthProvider>
        <div>child</div>
      </AuthProvider>,
    );
    expect(loadCurrentUser).toHaveBeenCalledTimes(1);
  });

  it("does not call loadCurrentUser again on re-render", () => {
    const { rerender } = render(
      <AuthProvider>
        <div>child1</div>
      </AuthProvider>,
    );
    expect(loadCurrentUser).toHaveBeenCalledTimes(1);

    rerender(
      <AuthProvider>
        <div>child2</div>
      </AuthProvider>,
    );
    expect(loadCurrentUser).toHaveBeenCalledTimes(1);
  });
});
