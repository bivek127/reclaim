import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import "./AppShell.css";

/**
 * Persistent frame for the console.
 *
 * Navigation is organised by operator task — what needs me, find a case,
 * decide, is the machine healthy — rather than by database table. The
 * per-case audit timeline is not a destination here: it belongs to a case,
 * and reconstruction is defined per case.
 */

const NAV = [
  { to: "/", label: "Overview", end: true, hint: "What needs attention now" },
  { to: "/cases", label: "Cases", hint: "Find and triage recovery cases" },
  { to: "/reviews", label: "Reviews", hint: "Decide on escalated cases" },
  { to: "/system", label: "System", hint: "Breaker and worker health" },
];

export function AppShell() {
  const meta = useQuery({
    queryKey: ["meta"],
    queryFn: api.meta,
    staleTime: 5 * 60 * 1000,
  });

  const reviews = useQuery({
    queryKey: ["reviews", "PENDING", "badge"],
    queryFn: () => api.reviews("PENDING", 1, 0),
    refetchInterval: 30_000,
  });

  const pending = reviews.data?.total ?? 0;
  const environment = meta.data?.environment;

  return (
    <div className="shell">
      <a className="shell__skip" href="#main">Skip to content</a>

      <aside className="shell__nav">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">Reclaim</span>
        </div>

        <nav aria-label="Sections">
          <ul className="nav">
            {NAV.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  title={item.hint}
                  className={({ isActive }) =>
                    `nav__item${isActive ? " nav__item--active" : ""}`
                  }
                >
                  <span>{item.label}</span>
                  {item.to === "/reviews" && pending > 0 && (
                    <span className="nav__count" aria-label={`${pending} pending`}>
                      {pending}
                    </span>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="shell__foot">
          {environment && (
            <div className="env" title="Database this console is attached to">
              <span className="env__dot" aria-hidden="true" />
              <span className="env__name">{environment}</span>
            </div>
          )}
        </div>
      </aside>

      <main className="shell__main" id="main">
        <Outlet />
      </main>
    </div>
  );
}
