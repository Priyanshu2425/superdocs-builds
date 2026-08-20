import { useState } from "react";
import { RepairError, styleDocument, type Stage, type Styled } from "../lib/repair";

/**
 * The optional second file.
 *
 * It sits below the download, never above it, and it is never automatic. The
 * plain rebuild is already in the person's hands by the time this appears —
 * everything here is an offer of something more, and every failure it can have
 * ends with them still holding what they already had.
 *
 * The waiting line is not decoration. The docs are explicit that a large
 * document can take minutes with nothing visible happening, and a page that
 * does not say so turns a slow success into a suspected crash.
 */
export function Styling({ url, available, note }: { url: string; available: boolean; note: string }) {
  const [phase, setPhase] = useState<"idle" | "working" | "done">("idle");
  const [stages, setStages] = useState<Stage[]>([]);
  const [styled, setStyled] = useState<Styled | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  if (!available) {
    return note ? <p className="styling__off">{note}</p> : null;
  }

  const run = async () => {
    setPhase("working");
    setStages([]);
    setProblem(null);
    try {
      const result = await styleDocument(url, (s) => setStages((prev) => [...prev, s]));
      setStyled(result);
      setPhase("done");
    } catch (e) {
      setProblem(
        e instanceof RepairError
          ? e.message
          : "Styling did not work. The rebuilt file above is unchanged and still yours.",
      );
      setPhase("done");
    }
  };

  const cost = styled
    ? `${styled.ops_charged} ${styled.ops_confirmed ? "" : "estimated "}${
        styled.ops_charged === 1 ? "operation" : "operations"
      }`
    : "";

  return (
    <section className="styling" aria-labelledby="styling-h">
      <h3 id="styling-h" className="outcome__head">
        A styled copy, if you want one
      </h3>
      <p className="styling__lead">
        The file above is plain on purpose. The fonts and spacing of your original could not be read
        out of a damaged file, and this page will not guess at a design you had. SuperDocs can lay
        the recovered headings, tables and paragraphs out with consistent styling instead. It is
        told to change no words, only how they are set. What comes back is compared against what
        went out, word for word, and thrown away if the wording moved at all — a recovered document
        that reads well but says something you did not write is worse than a plain one. The plain
        file above is untouched whatever happens.
      </p>

      {phase === "idle" ? (
        <>
          <button type="button" className="btn btn--quiet" onClick={() => void run()}>
            Send it for styling
          </button>
          <p className="keep">
            This sends the recovered text to SuperDocs. If you would rather it stayed on this
            machine, the file above is already yours and nothing more needs to happen.
          </p>
        </>
      ) : null}

      {phase !== "idle" ? (
        <ul className="stations" aria-label="What this page is doing">
          {stages.map((s, i) => (
            <li
              className={`station${
                phase === "done" || i < stages.length - 1 ? " station--done" : ""
              }`}
              key={`${s.stage}-${i}`}
            >
              <span className="station__mark" aria-hidden="true" />
              <span className="station__text">{s.message}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {phase === "working" ? (
        <p className="working">
          <span>Still working. A long document can take a few minutes, and a quiet wait is
          normal.</span>
          <span className="working__bar" aria-hidden="true" />
        </p>
      ) : null}

      <p aria-live="polite" className="sr-only">
        {phase === "working"
          ? (stages[stages.length - 1]?.message ?? "Working.")
          : phase === "done"
            ? (problem ?? styled?.notes[styled.notes.length - 1] ?? "")
            : ""}
      </p>

      {phase === "done" && styled?.ok && styled.download ? (
        <div className="handoff">
          <div className="tape" />
          <div className="handoff__body">
            <a className="btn" href={styled.download} download>
              Download the styled file
            </a>
            <p className="keep">
              Saved as <b>{styled.filename}</b>. It carries the same recovered content as the file
              above, laid out with consistent styling — it recovers nothing extra. That cost{" "}
              {cost}. The plain file above is untouched and still yours.
              {styled.warnings > 0 ? (
                <>
                  {" "}
                  The export reported {styled.warnings} non-fatal{" "}
                  {styled.warnings === 1 ? "warning" : "warnings"}, so compare it against the plain
                  file before you rely on it.
                </>
              ) : null}
            </p>
          </div>
        </div>
      ) : null}

      {phase === "done" && !styled?.ok ? (
        <p className="problem" role="alert">
          <b>{styled?.rejected_for_content ? "Thrown away. " : "No styled copy. "}</b>
          {problem ?? styled?.notes[styled.notes.length - 1] ?? ""}
        </p>
      ) : null}
    </section>
  );
}
