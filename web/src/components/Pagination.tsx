import "./Pagination.css";

interface Props {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}

/**
 * Offset paging over a bounded page size.
 *
 * Shows the range rather than a page count alone, because an operator working
 * a queue needs to know how much is left, not which page they are on.
 */
export function Pagination({ page, pageSize, total, onPage }: Props) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total === 0) return null;

  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <nav className="pager" aria-label="Pagination">
      <p className="pager__range">
        <strong>{first}–{last}</strong> of {total}
      </p>
      <div className="pager__controls">
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onPage(page - 1)}
          disabled={page <= 1}
        >
          Previous
        </button>
        <span className="pager__page">Page {page} of {pages}</span>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onPage(page + 1)}
          disabled={page >= pages}
        >
          Next
        </button>
      </div>
    </nav>
  );
}
