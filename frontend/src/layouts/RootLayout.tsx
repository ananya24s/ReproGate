import { Outlet } from "react-router-dom";

import { AppHeader } from "@/components/layout/AppHeader";

/** Chrome shared by every route: header, main content region, and footer. */
export function RootLayout() {
  return (
    <div className="flex min-h-screen flex-col">
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-10">
        <Outlet />
      </main>
      <footer className="border-t border-border">
        <div className="mx-auto max-w-5xl px-6 py-4 text-xs text-muted-foreground">
          Deterministic execution inside Docker is the source of truth.
        </div>
      </footer>
    </div>
  );
}
