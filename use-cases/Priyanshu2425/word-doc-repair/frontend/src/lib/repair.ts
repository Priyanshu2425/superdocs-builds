/**
 * The one conversation this page has with the server.
 *
 * `POST /api/repair` answers with newline-delimited JSON: a stage line each
 * time the engine reaches one, then a final line carrying the report. Reading
 * it as a stream is the point -- someone watching their document being read
 * needs to see it happening, not a spinner that could mean anything.
 */

export interface Stage {
  stage: string;
  message: string;
}

export interface Report {
  ok: boolean;
  summary: string;
  recovered: string[];
  lost: string[];
  counts: Record<string, number>;
  structure_preserved: boolean;
  filename: string;
  /** The rebuilt document, rendered for reading. Empty on a failure, because
   *  there is nothing to preview and a preview of nothing is a claim. */
  preview_html: string;
  download: string | null;
  /** Where the optional styling pass can be started. Null on a failure. */
  style: string | null;
}

/** What this copy of the page can do. Asked before anything is offered, so the
 *  styling step is either genuinely available or plainly explained. */
export interface Capabilities {
  styling: boolean;
  note: string;
}

export interface Styled {
  ok: boolean;
  notes: string[];
  /** A styled file came back and was thrown away because its wording had
   *  changed. Not a failure of the service so much as a refusal by this page. */
  rejected_for_content: boolean;
  filename: string;
  download: string | null;
  ops_charged: number;
  ops_confirmed: boolean;
  allowance_known: boolean;
  allowance_remaining: number;
  warnings: number;
}

export const MAX_BYTES = 20 * 1024 * 1024;

export class RepairError extends Error {}

/** What we can tell before sending anything, said in the reader's words. */
export function checkFile(file: File): string | null {
  if (file.size === 0) {
    return "That file is empty — there is nothing inside it to recover. If you have another copy, even an older one, try that instead.";
  }
  if (file.size > MAX_BYTES) {
    return "That file is larger than 20 MB, which is more than this page can take.";
  }
  if (!/\.docx$/i.test(file.name)) {
    // The advice that used to stand here was "open it in Word and save it as
    // .docx" -- addressed to somebody whose Word will not open the file, which
    // is why they are here. It is still the right first move when the file is
    // merely old, so it stays; what was missing is the other half.
    return "This page can only work on Word .docx files. If yours is a .doc, open it in Word once and save it as .docx — and if Word will not open it either, this page cannot recover that older format.";
  }
  return null;
}

export async function capabilities(): Promise<Capabilities> {
  try {
    const res = await fetch("/api/capabilities");
    if (!res.ok) throw new Error("unavailable");
    return (await res.json()) as Capabilities;
  } catch {
    // A page that cannot ask does not offer. Silence here is not "yes".
    return { styling: false, note: "" };
  }
}

/**
 * The second pass, and only when somebody asks for it.
 *
 * Answers in the same newline-delimited stream as the repair, because it is the
 * same situation: something slow is happening to a person's document and they
 * are entitled to watch it. A long silence here is still processing, which is
 * what the waiting line on the page says.
 */
export async function styleDocument(
  url: string,
  onStage: (stage: Stage) => void,
  signal?: AbortSignal,
): Promise<Styled> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", signal });
  } catch {
    throw new RepairError(
      "The connection dropped. The rebuilt file above is unchanged and still yours.",
    );
  }
  if (!res.ok || !res.body) {
    let detail = "";
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* nothing more to learn */
    }
    throw new RepairError(
      detail || "Styling could not be started. The rebuilt file above is unchanged and still yours.",
    );
  }
  const final = await readStream(res, onStage);
  if (!final) {
    throw new RepairError(
      "The connection dropped part-way through. The rebuilt file above is unchanged and still yours.",
    );
  }
  return final as unknown as Styled;
}

/** One reader for both streams: stage lines, then a final line marked done. */
async function readStream(
  res: Response,
  onStage: (stage: Stage) => void,
): Promise<Record<string, unknown> | null> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: Record<string, unknown> | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let cut: number;
    while ((cut = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      if (!line) continue;
      const parsed = JSON.parse(line) as Record<string, unknown>;
      if (parsed.done) final = parsed;
      else onStage(parsed as unknown as Stage);
    }
  }
  return final;
}

export async function repairFile(
  file: File,
  onStage: (stage: Stage) => void,
  signal?: AbortSignal,
): Promise<Report> {
  const form = new FormData();
  form.append("file", file, file.name);

  let res: Response;
  try {
    res = await fetch("/api/repair", { method: "POST", body: form, signal });
  } catch {
    throw new RepairError(
      "The connection dropped before your file could be read. Nothing was changed — your original is exactly as it was.",
    );
  }

  if (!res.ok || !res.body) {
    let detail = "";
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* nothing more to learn */
    }
    throw new RepairError(
      detail || "This page could not take that file just now. Nothing was changed.",
    );
  }

  const report = (await readStream(res, onStage)) as unknown as Report | null;

  if (!report) {
    throw new RepairError(
      "The connection dropped part-way through. Nothing was changed — your original is exactly as it was.",
    );
  }
  return report;
}
