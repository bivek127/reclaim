import { useCallback, useState } from "react";

/**
 * The reviewer identifier recorded against a decision.
 *
 * This system has no authentication layer, so the identifier is supplied by
 * the operator and remembered on this device for convenience. It is an audit
 * label, not a credential, and the UI says so wherever it is collected —
 * pretending otherwise would misrepresent what the audit trail proves.
 */

const KEY = "reclaim.reviewer_ref";

function read(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function useReviewer() {
  const [reviewer, setReviewerState] = useState<string>(read);

  const setReviewer = useCallback((value: string) => {
    setReviewerState(value);
    try {
      if (value.trim()) window.localStorage.setItem(KEY, value.trim());
      else window.localStorage.removeItem(KEY);
    } catch {
      // Storage unavailable; the value still applies for this session.
    }
  }, []);

  return { reviewer, setReviewer, hasReviewer: reviewer.trim().length > 0 };
}
