"use client";

import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth";

/**
 * Client-side auth initializer.
 *
 * Mounts once at the top of the layout tree and calls /auth/me to
 * restore the session from cookies.  Renders nothing — all state is
 * managed by the Zustand auth store.
 */
export default function AuthProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const loadCurrentUser = useAuthStore((s) => s.loadCurrentUser);

  useEffect(() => {
    loadCurrentUser();
  }, [loadCurrentUser]);

  return <>{children}</>;
}
