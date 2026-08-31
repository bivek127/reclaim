import { createBrowserRouter } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { OverviewPage } from "@/pages/OverviewPage";
import { CasesPage } from "@/pages/CasesPage";
import { CaseDetailPage } from "@/pages/CaseDetailPage";
import { ReviewsPage } from "@/pages/ReviewsPage";
import { ReviewDetailPage } from "@/pages/ReviewDetailPage";
import { UnmappablePage } from "@/pages/UnmappablePage";
import { SystemPage } from "@/pages/SystemPage";
import { NotFoundPage } from "@/pages/NotFoundPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "cases", element: <CasesPage /> },
      { path: "cases/:caseId", element: <CaseDetailPage /> },
      { path: "cases/:caseId/timeline", element: <CaseDetailPage /> },
      { path: "reviews", element: <ReviewsPage /> },
      { path: "reviews/:caseId", element: <ReviewDetailPage /> },
      { path: "unmappable", element: <UnmappablePage /> },
      { path: "system", element: <SystemPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);
