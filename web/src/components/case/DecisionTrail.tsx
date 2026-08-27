import { Disclosure, TechnicalRows } from "../Disclosure";
import { absolute, relativeFromNow } from "@/lib/time";
import type { CaseDetail, Diagnosis, PolicyDecision } from "@/lib/types";
import "./DecisionTrail.css";

/**
 * Why the system acted.
 *
 * Diagnosis and policy are rendered as separate, unequal things. Diagnosis is
 * advisory input; the deterministic policy table is what authorises an action.
 * Ambiguity is evaluated from the cause lookup and supplied history, never
 * from model confidence — so confidence is shown as a small annotation on the
 * advisory block and never near a verdict or an amount.
 */

const VERDICT_NOTE: Record<string, string> = {
  ALLOW: "The policy table permitted a recovery action.",
  ESCALATE: "The policy table refused to act automatically and handed the case to a human.",
  NO_ACTION: "The policy table permitted no action for this cause.",
};

export function PolicyTrail({ decisions }: { decisions: PolicyDecision[] }) {
  if (decisions.length === 0) {
    return <p className="trail__empty">No policy decision was recorded for this case.</p>;
  }
  return (
    <ol className="trail">
      {decisions.map((p, i) => (
        <li key={p.id} className="trail__item">
          <div className="trail__head">
            <span className="trail__step">Decision {i + 1}</span>
            <span className={`trail__verdict verdict-${p.verdict.toLowerCase()}`}>{p.verdict}</span>
            {p.selected_action && <span className="trail__action u-mono">{p.selected_action}</span>}
            <span className="trail__time" title={absolute(p.created_at)}>
              {relativeFromNow(p.created_at)}
            </span>
          </div>

          <p className="trail__reason">
            <span className="u-mono trail__code">{p.reason_code}</span>
            <span className="trail__note">{VERDICT_NOTE[p.verdict] ?? ""}</span>
          </p>

          {/* The deterministic inputs, stated plainly so the verdict is checkable. */}
          <dl className="trail__inputs">
            <div>
              <dt>Cause in policy table</dt>
              <dd>{p.lookup_miss ? "No — unmapped cause" : "Yes"}</dd>
            </div>
            <div>
              <dt>Conflicting history</dt>
              <dd>{p.conflicting_history ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Ambiguity signal</dt>
              <dd className={p.ambiguity_signal ? "is-flagged" : undefined}>
                {p.ambiguity_signal ? "Raised" : "Not raised"}
              </dd>
            </div>
            <div>
              <dt>Policy version</dt>
              <dd className="u-mono">{p.policy_version}</dd>
            </div>
          </dl>

          <Disclosure label="Decision details">
            <TechnicalRows
              rows={[
                ["Policy decision id", String(p.id)],
                ["Diagnosis id", p.diagnosis_id ? String(p.diagnosis_id) : "—"],
                ["Recorded", absolute(p.created_at)],
              ]}
            />
          </Disclosure>
        </li>
      ))}
    </ol>
  );
}

export function DiagnosisTrail({ diagnoses }: { diagnoses: Diagnosis[] }) {
  if (diagnoses.length === 0) {
    return <p className="trail__empty">No diagnosis was recorded for this case.</p>;
  }
  return (
    <ul className="trail">
      {diagnoses.map((d) => {
        const fromModel = d.source === "LLM";
        return (
          <li key={d.id} className="trail__item trail__item--advisory">
            <div className="trail__head">
              <span className="dx__cause u-mono">{d.cause}</span>
              <span className="dx__source">
                {fromModel ? `Model · ${d.model ?? "unnamed"}` : "Deterministic fallback"}
              </span>
              {d.confidence !== null && (
                <span className="dx__confidence" title="Model self-reported confidence. It does not influence policy.">
                  confidence {d.confidence}
                </span>
              )}
              <span className="trail__time" title={absolute(d.created_at)}>
                {relativeFromNow(d.created_at)}
              </span>
            </div>

            {d.reasoning && <p className="dx__reasoning">{d.reasoning}</p>}

            {d.recommended_action && (
              <p className="dx__recommend">
                Suggested <span className="u-mono">{d.recommended_action}</span> — a suggestion
                only; the policy table decides what is permitted.
              </p>
            )}

            <Disclosure label="Diagnosis details">
              <TechnicalRows
                rows={[
                  ["Diagnosis id", String(d.id)],
                  ["Source", d.source],
                  ["Model", d.model ?? "—"],
                  ["Model version", d.model_version ?? "—"],
                  ["Prompt version", d.prompt_version],
                  ["Confidence", d.confidence === null ? "—" : String(d.confidence)],
                  ["Retries", String(d.llm_retry_count)],
                  ["Recorded", absolute(d.created_at)],
                ]}
              />
            </Disclosure>
          </li>
        );
      })}
    </ul>
  );
}

/** Compact restatement of the authorisation chain, in the order it happened. */
export function DecisionChain({ detail }: { detail: CaseDetail }) {
  const last = detail.policy_decisions[detail.policy_decisions.length - 1];
  const dx = detail.diagnoses[0];
  if (!last && !dx) return null;
  return (
    <ol className="chain" aria-label="How the action was authorised">
      <li className="chain__node chain__node--advisory">
        <span className="chain__label">Diagnosis</span>
        <span className="chain__value u-mono">{dx?.cause ?? "none"}</span>
        <span className="chain__tag">advisory</span>
      </li>
      <li className="chain__node">
        <span className="chain__label">Ambiguity check</span>
        <span className="chain__value">{last?.ambiguity_signal ? "Raised" : "Not raised"}</span>
        <span className="chain__tag">deterministic</span>
      </li>
      <li className="chain__node">
        <span className="chain__label">Policy verdict</span>
        <span className="chain__value">{last?.verdict ?? "none"}</span>
        <span className="chain__tag">authoritative</span>
      </li>
      <li className="chain__node">
        <span className="chain__label">Selected action</span>
        <span className="chain__value u-mono">{last?.selected_action ?? "none"}</span>
      </li>
    </ol>
  );
}
