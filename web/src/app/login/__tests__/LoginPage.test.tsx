import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockPush = vi.fn();
const mockReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("next/link", () => ({
  default: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => (
    <a {...props}>{children}</a>
  ),
}));

const mockLogin = vi.fn();
vi.mock("@/stores/auth", () => ({
  useAuthStore: Object.assign(
    (selector: (s: { login: typeof mockLogin; user: null }) => unknown) =>
      selector({ login: mockLogin, user: null }),
    { setState: vi.fn() },
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual,
      fetchCsrf: vi.fn(),
    },
    getReturnUrl: () => "/",
  };
});

import { api, ApiError } from "@/lib/api";
import LoginPage from "@/app/login/page";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchCsrf).mockResolvedValue("csrf");
  mockLogin.mockResolvedValue(undefined);
});

describe("LoginPage", () => {
  it("renders username and password fields", () => {
    render(<LoginPage />);
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  });

  it("renders Login button", () => {
    render(<LoginPage />);
    expect(screen.getByRole("button", { name: /login/i })).toBeInTheDocument();
  });

  it("renders Create account link to /register", () => {
    render(<LoginPage />);
    const link = screen.getByRole("link", { name: /create account/i });
    expect(link).toHaveAttribute("href", "/register");
  });

  it("shows error message on 401", async () => {
    const user = userEvent.setup();
    mockLogin.mockRejectedValueOnce(new ApiError(401, "Invalid credentials"));

    render(<LoginPage />);
    await user.type(screen.getByLabelText(/username/i), "user");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /login/i }));

    await waitFor(() => {
      expect(screen.getByText(/invalid username or password/i)).toBeInTheDocument();
    });
  });

  it("calls login on form submission", async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await user.type(screen.getByLabelText(/username/i), "user");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /login/i }));

    expect(mockLogin).toHaveBeenCalledWith({ username: "user", password: "password123" });
  });

  it("shows loading state during submission", async () => {
    const user = userEvent.setup();
    let resolveLogin!: () => void;
    mockLogin.mockReturnValueOnce(new Promise((r) => { resolveLogin = r; }));

    render(<LoginPage />);
    await user.type(screen.getByLabelText(/username/i), "user");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /login/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /logging in/i })).toBeDisabled();
    });

    resolveLogin();
  });

  it("redirects to returnUrl after successful login", async () => {
    const user = userEvent.setup();
    render(<LoginPage />);
    await user.type(screen.getByLabelText(/username/i), "user");
    await user.type(screen.getByLabelText(/password/i), "password123");
    await user.click(screen.getByRole("button", { name: /login/i }));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("/");
    });
  });
});
