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
    return "This page can only work on Word .docx files. If your file is a .doc, open it in Word once and save it as .docx first.";
  }
  return null;
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

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let report: Report | null = null;

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
      if (parsed.done) report = parsed as unknown as Report;
      else onStage(parsed as unknown as Stage);
    }
  }

  if (!report) {
    throw new RepairError(
      "The connection dropped part-way through. Nothing was changed — your original is exactly as it was.",
    );
  }
  return report;
}
