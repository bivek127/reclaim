import { Link } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/States";
import { overviewPath } from "@/lib/routes";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <div className="page-body">
        <EmptyState
          title="That page does not exist"
          description="The link may be out of date."
          action={<Link className="btn btn--secondary" to={overviewPath()}>Back to overview</Link>}
        />
      </div>
    </>
  );
}
