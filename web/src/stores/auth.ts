/**
 * Client-side authentication state.
 *
 * Stores only the safe User object — never tokens.
 * All token management is delegated to httpOnly cookies.
 */

import { create } from "zustand";
import type { User } from "@/lib/types";
import { api } from "@/lib/api";

interface AuthState {
  /** The authenticated user, or null. */
  user: User | null;
  /** True while the initial /auth/me check is in flight. */
  isInitializing: boolean;
  /** True when a login/register/logout/refresh is in progress. */
  isLoading: boolean;

  /** Check the session and load the current user. Called once on mount. */
  loadCurrentUser: () => Promise<void>;
  /** Register a new account, then auto-login. */
  register: (data: {
    username: string;
    password: string;
    email?: string;
  }) => Promise<void>;
  /** Login with username and password. */
  login: (data: { username: string; password: string }) => Promise<void>;
  /** Logout and clear local state. */
  logout: () => Promise<void>;
  /** Force-refresh the user from the server. */
  refresh: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isInitializing: true,
  isLoading: false,

  loadCurrentUser: async () => {
    try {
      const user = await api.getMe();
      set({ user, isInitializing: false });
    } catch {
      set({ user: null, isInitializing: false });
    }
  },

  register: async (data) => {
    set({ isLoading: true });
    try {
      // Fetch pre-auth CSRF token first
      await api.fetchCsrf();
      await api.register(data);
      // After registration, login automatically
      await api.fetchCsrf();
      const res = await api.login({
        username: data.username,
        password: data.password,
      });
      set({ user: res.user, isLoading: false });
    } catch (err) {
      set({ isLoading: false });
      throw err;
    }
  },

  login: async (data) => {
    set({ isLoading: true });
    try {
      // Fetch pre-auth CSRF token first
      await api.fetchCsrf();
      const res = await api.login(data);
      set({ user: res.user, isLoading: false });
    } catch (err) {
      set({ isLoading: false });
      throw err;
    }
  },

  logout: async () => {
    set({ isLoading: true });
    try {
      await api.logout();
    } catch {
      // Clear state even if the server call fails (session may already be invalid)
    } finally {
      set({ user: null, isLoading: false });
    }
  },

  refresh: async () => {
    try {
      const user = await api.getMe();
      set({ user });
    } catch {
      set({ user: null });
    }
  },
}));
