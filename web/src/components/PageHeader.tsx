import type { ReactNode } from "react";
import "./PageHeader.css";

interface Props {
  title: string;
  description?: string;
  actions?: ReactNode;
  children?: ReactNode;
}

/** Consistent page framing: title, one line of orientation, primary actions. */
export function PageHeader({ title, description, actions, children }: Props) {
  return (
    <header className="page-header">
      <div className="page-header__row">
        <div className="page-header__text">
          <h1 className="page-header__title">{title}</h1>
          {description && <p className="page-header__desc">{description}</p>}
        </div>
        {actions && <div className="page-header__actions">{actions}</div>}
      </div>
      {children}
    </header>
  );
}
