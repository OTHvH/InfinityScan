import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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

const mockRegister = vi.fn();
vi.mock("@/stores/auth", () => ({
  useAuthStore: Object.assign(
    (selector: (s: { register: typeof mockRegister; user: null }) => unknown) =>
      selector({ register: mockRegister, user: null }),
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
import RegisterPage from "@/app/register/page";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchCsrf).mockResolvedValue("csrf");
  mockRegister.mockResolvedValue(undefined);
});

async function fillForm(
  user: ReturnType<typeof userEvent.setup>,
  opts: { username?: string; email?: string; password?: string; confirm?: string } = {},
) {
  if (opts.username) await user.type(screen.getByLabelText(/username/i), opts.username);
  if (opts.email) await user.type(screen.getByLabelText(/email/i), opts.email);
  if (opts.password) await user.type(screen.getByLabelText(/^password/i), opts.password);
  if (opts.confirm) await user.type(screen.getByLabelText(/confirm password/i), opts.confirm);
}

describe("RegisterPage", () => {
  it("renders all form fields", () => {
    render(<RegisterPage />);
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
  });

  it("renders Register button", () => {
    render(<RegisterPage />);
    expect(screen.getByRole("button", { name: /register/i })).toBeInTheDocument();
  });

  it("shows validation error for short username", async () => {
    const user = userEvent.setup();
    render(<RegisterPage />);
    await fillForm(user, { username: "ab", password: "password123", confirm: "password123" });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByText(/at least 3 characters/i)).toBeInTheDocument();
    });
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("shows validation error for invalid username chars", async () => {
    const user = userEvent.setup();
    render(<RegisterPage />);
    await fillForm(user, { username: "user name!", password: "password123", confirm: "password123" });
    // Use fireEvent.submit to bypass HTML pattern constraint validation in jsdom
    fireEvent.submit(screen.getByRole("button", { name: /register/i }).closest("form")!);

    await waitFor(() => {
      expect(screen.getByText(/letters, numbers, and underscores/i)).toBeInTheDocument();
    });
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("shows validation error for short password", async () => {
    const user = userEvent.setup();
    render(<RegisterPage />);
    await fillForm(user, { username: "validuser", password: "short", confirm: "short" });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByText(/at least 8 characters/i)).toBeInTheDocument();
    });
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("shows validation error for mismatched passwords", async () => {
    const user = userEvent.setup();
    render(<RegisterPage />);
    await fillForm(user, {
      username: "validuser",
      password: "password123",
      confirm: "password456",
    });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByText(/passwords do not match/i)).toBeInTheDocument();
    });
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("calls register on valid form submission", async () => {
    const user = userEvent.setup();
    render(<RegisterPage />);
    await fillForm(user, {
      username: "validuser",
      email: "test@example.com",
      password: "password123",
      confirm: "password123",
    });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(mockRegister).toHaveBeenCalledWith({
        username: "validuser",
        password: "password123",
        email: "test@example.com",
      });
    });
  });

  it("shows 'Username already taken' on 409 with that detail", async () => {
    const user = userEvent.setup();
    mockRegister.mockRejectedValueOnce(
      new ApiError(409, "Username already taken"),
    );
    render(<RegisterPage />);
    await fillForm(user, {
      username: "taken",
      password: "password123",
      confirm: "password123",
    });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByText(/username is already taken/i)).toBeInTheDocument();
    });
  });

  it("shows 'Email already registered' on 409 with that detail", async () => {
    const user = userEvent.setup();
    mockRegister.mockRejectedValueOnce(
      new ApiError(409, "Email already registered"),
    );
    render(<RegisterPage />);
    await fillForm(user, {
      username: "validuser",
      email: "dup@example.com",
      password: "password123",
      confirm: "password123",
    });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByText(/email is already registered/i)).toBeInTheDocument();
    });
  });

  it("shows loading state during submission", async () => {
    const user = userEvent.setup();
    let resolveRegister!: () => void;
    mockRegister.mockReturnValueOnce(new Promise<void>((resolve) => { resolveRegister = resolve; }));

    render(<RegisterPage />);
    await fillForm(user, {
      username: "validuser",
      password: "password123",
      confirm: "password123",
    });
    await user.click(screen.getByRole("button", { name: /register/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /creating account/i })).toBeDisabled();
    });

    resolveRegister();
  });
});
