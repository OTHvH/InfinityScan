import { describe, it, expect, vi, beforeEach } from "vitest";
import type { User } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    getMe: vi.fn(),
    fetchCsrf: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(detail);
      this.name = "ApiError";
      this.status = status;
      this.detail = detail;
    }
  },
}));

import { api, ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/auth";

const mockUser: User = {
  id: "1",
  username: "testuser",
  email: "test@example.com",
  role: "user",
  is_active: true,
  created_at: "2024-01-01T00:00:00Z",
};

const mockedApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({ user: null, isInitializing: true, isLoading: false });
});

describe("loadCurrentUser", () => {
  it("calls /auth/me and sets user on success", async () => {
    mockedApi.getMe.mockResolvedValueOnce(mockUser);
    await useAuthStore.getState().loadCurrentUser();
    expect(useAuthStore.getState().user).toEqual(mockUser);
    expect(mockedApi.getMe).toHaveBeenCalledTimes(1);
  });

  it("sets user to null on failure", async () => {
    mockedApi.getMe.mockRejectedValueOnce(new Error("network"));
    await useAuthStore.getState().loadCurrentUser();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("sets isInitializing: false after completion", async () => {
    mockedApi.getMe.mockResolvedValueOnce(mockUser);
    expect(useAuthStore.getState().isInitializing).toBe(true);
    await useAuthStore.getState().loadCurrentUser();
    expect(useAuthStore.getState().isInitializing).toBe(false);
  });

  it("sets isInitializing: false even on failure", async () => {
    mockedApi.getMe.mockRejectedValueOnce(new Error("network"));
    await useAuthStore.getState().loadCurrentUser();
    expect(useAuthStore.getState().isInitializing).toBe(false);
  });
});

describe("login", () => {
  it("fetches CSRF token first, then calls login", async () => {
    mockedApi.fetchCsrf.mockResolvedValueOnce("csrf-token");
    mockedApi.login.mockResolvedValueOnce({ user: mockUser });
    await useAuthStore.getState().login({ username: "test", password: "pass1234" });
    expect(mockedApi.fetchCsrf).toHaveBeenCalledTimes(1);
    expect(mockedApi.login).toHaveBeenCalledWith({ username: "test", password: "pass1234" });
  });

  it("sets user from response", async () => {
    mockedApi.fetchCsrf.mockResolvedValueOnce("csrf-token");
    mockedApi.login.mockResolvedValueOnce({ user: mockUser });
    await useAuthStore.getState().login({ username: "test", password: "pass1234" });
    expect(useAuthStore.getState().user).toEqual(mockUser);
  });

  it("sets isLoading during operation", async () => {
    let resolveLogin!: (v: { user: User }) => void;
    mockedApi.fetchCsrf.mockResolvedValueOnce("csrf-token");
    mockedApi.login.mockReturnValueOnce(
      new Promise((r) => {
        resolveLogin = r;
      }),
    );

    const loginPromise = useAuthStore.getState().login({ username: "test", password: "pass1234" });

    // Wait a tick for state updates
    await new Promise((r) => setTimeout(r, 10));
    expect(useAuthStore.getState().isLoading).toBe(true);

    resolveLogin({ user: mockUser });
    await loginPromise;
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("re-throws errors after setting isLoading: false", async () => {
    mockedApi.fetchCsrf.mockResolvedValueOnce("csrf-token");
    mockedApi.login.mockRejectedValueOnce(new ApiError(401, "Invalid credentials"));

    await expect(
      useAuthStore.getState().login({ username: "test", password: "wrong" }),
    ).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().isLoading).toBe(false);
  });
});

describe("register", () => {
  it("fetches CSRF, registers, fetches CSRF again, then logs in", async () => {
    mockedApi.fetchCsrf
      .mockResolvedValueOnce("csrf1")
      .mockResolvedValueOnce("csrf2");
    mockedApi.register.mockResolvedValueOnce({ user: mockUser });
    mockedApi.login.mockResolvedValueOnce({ user: mockUser });

    await useAuthStore.getState().register({
      username: "newuser",
      password: "pass1234",
      email: "new@example.com",
    });

    expect(mockedApi.fetchCsrf).toHaveBeenCalledTimes(2);
    expect(mockedApi.register).toHaveBeenCalledWith({
      username: "newuser",
      password: "pass1234",
      email: "new@example.com",
    });
    expect(mockedApi.login).toHaveBeenCalledWith({
      username: "newuser",
      password: "pass1234",
    });
  });

  it("sets user from login response", async () => {
    mockedApi.fetchCsrf.mockResolvedValue("csrf");
    mockedApi.register.mockResolvedValueOnce({ user: mockUser });
    mockedApi.login.mockResolvedValueOnce({ user: mockUser });

    await useAuthStore.getState().register({
      username: "newuser",
      password: "pass1234",
    });
    expect(useAuthStore.getState().user).toEqual(mockUser);
  });

  it("re-throws errors", async () => {
    mockedApi.fetchCsrf.mockResolvedValueOnce("csrf");
    mockedApi.register.mockRejectedValueOnce(
      new ApiError(400, "Username already taken"),
    );

    await expect(
      useAuthStore.getState().register({
        username: "taken",
        password: "pass1234",
      }),
    ).rejects.toMatchObject({ detail: "Username already taken" });
    expect(useAuthStore.getState().isLoading).toBe(false);
  });
});

describe("logout", () => {
  it("calls api.logout()", async () => {
    mockedApi.logout.mockResolvedValueOnce(undefined);
    await useAuthStore.getState().logout();
    expect(mockedApi.logout).toHaveBeenCalledTimes(1);
  });

  it("sets user to null", async () => {
    useAuthStore.setState({ user: mockUser });
    mockedApi.logout.mockResolvedValueOnce(undefined);
    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("clears state even if server call fails", async () => {
    useAuthStore.setState({ user: mockUser });
    mockedApi.logout.mockRejectedValueOnce(new Error("network"));
    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().isLoading).toBe(false);
  });
});

describe("refresh", () => {
  it("calls getMe() and sets user", async () => {
    mockedApi.getMe.mockResolvedValueOnce(mockUser);
    await useAuthStore.getState().refresh();
    expect(useAuthStore.getState().user).toEqual(mockUser);
  });

  it("sets user to null on failure", async () => {
    useAuthStore.setState({ user: mockUser });
    mockedApi.getMe.mockRejectedValueOnce(new Error("expired"));
    await useAuthStore.getState().refresh();
    expect(useAuthStore.getState().user).toBeNull();
  });
});

describe("No tokens stored", () => {
  it("store never contains access_token or refresh_token fields", async () => {
    mockedApi.getMe.mockResolvedValueOnce(mockUser);
    await useAuthStore.getState().loadCurrentUser();
    const state = useAuthStore.getState();
    expect(state).not.toHaveProperty("access_token");
    expect(state).not.toHaveProperty("refresh_token");
    expect(state).not.toHaveProperty("accessToken");
    expect(state).not.toHaveProperty("refreshToken");
  });
});
