import { ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";

import { paths } from "@/routes/paths";

export function AppHeader() {
  return (
    <header className="border-b border-border">
      <div className="mx-auto flex h-14 max-w-5xl items-center px-6">
        <Link
          to={paths.home}
          className="flex items-center gap-2 text-sm font-semibold tracking-tight"
        >
          <ShieldCheck className="size-4" aria-hidden="true" />
          ReproGate
        </Link>
      </div>
    </header>
  );
}
