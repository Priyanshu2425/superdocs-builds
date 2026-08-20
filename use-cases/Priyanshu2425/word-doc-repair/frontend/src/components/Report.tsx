import type { Report as ReportT } from "../lib/repair";
import { Preview } from "./Preview";

/**
 * What happened, in the order a frightened person needs it: the verdict first,
 * then what came through, then what did not, then the file.
 *
 * A failed repair renders no "what came through" claim at all. That was BUG-012:
 * a total failure that still showed the heading is the precise overclaim this
 * build's README says it does not make.
 */
export function Report({
  report,
  onAnother,
}: {
  report: ReportT;
  onAnother: () => void;
}) {
  const partial = report.ok && report.lost.length > 0;
  const stamp = !report.ok ? "Could not be repaired" : partial ? "Recovered in part" : "Recovered";

  return (
    <section aria-labelledby="verdict-h">
      <h2 id="verdict-h" className="sr-only">
        What happened to your document
      </h2>
      <p style={{ margin: "6px 0 0" }}>
        <span
          className={`stamp${!report.ok ? " stamp--failed" : partial ? " stamp--part" : ""}`}
        >
          {stamp}
        </span>
      </p>
      <p className="verdict">{report.summary}</p>

      {report.ok && report.recovered.length > 0 ? (
        <div className="outcome outcome--kept">
          <h3 className="outcome__head">What came through</h3>
          <ul className="outcome__list outcome--kept">
            {report.recovered.map((line) => (
              <li key={line}>
                <i aria-hidden="true">✓</i>
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.lost.length > 0 ? (
        <div className="outcome outcome--lost">
          <h3 className="outcome__head">
            {report.ok ? "What did not come through" : "What went wrong"}
          </h3>
          <ul className="outcome__list outcome--lost">
            {report.lost.map((line) => (
              <li key={line}>
                <i aria-hidden="true">×</i>
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.ok ? <Preview html={report.preview_html} /> : null}

      {report.ok && report.download ? (
        <div className="handoff">
          <div className="tape" />
          <div className="handoff__body">
            <a className="btn" href={report.download} download>
              Download the repaired file
            </a>
            <p className="keep">
              Saved as <b>{report.filename}</b>. You can download it more than once. This page holds
              it for about thirty minutes and then lets it go — if the link stops working, repair the
              document again. Your original was never changed.
            </p>
          </div>
        </div>
      ) : null}

      <div className="after">
        <button type="button" className="btn--quiet btn" onClick={onAnother}>
          Repair another document
        </button>
      </div>
    </section>
  );
}
