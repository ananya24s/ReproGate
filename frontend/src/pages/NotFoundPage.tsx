import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { paths } from "@/routes/paths";

export function NotFoundPage() {
  return (
    <section>
      <h1 className="text-2xl font-semibold tracking-tight">Page not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The page you requested does not exist.
      </p>
      <Button asChild className="mt-6">
        <Link to={paths.home}>Back to start</Link>
      </Button>
    </section>
  );
}
