import { createBrowserRouter } from "react-router-dom";

import { RootLayout } from "@/layouts/RootLayout";
import { CreateVerificationRunPage } from "@/pages/CreateVerificationRunPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { VerificationReportPage } from "@/pages/VerificationReportPage";
import { VerificationRunPage } from "@/pages/VerificationRunPage";
import { paths } from "@/routes/paths";

export const router = createBrowserRouter([
  {
    path: paths.home,
    element: <RootLayout />,
    children: [
      { index: true, element: <CreateVerificationRunPage /> },
      { path: paths.verificationRun(), element: <VerificationRunPage /> },
      {
        path: paths.verificationRunReport(),
        element: <VerificationReportPage />,
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
