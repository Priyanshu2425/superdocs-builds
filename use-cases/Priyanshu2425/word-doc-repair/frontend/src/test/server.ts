/**
 * The fake this page is tested against streams real repairs.
 *
 * Every line it sends was produced by the engine and recorded by
 * tests/test_frontend_fixtures.py, which fails if the recording drifts. A page
 * that tells somebody what happened to their document must be tested against
 * what actually happens to documents.
 */
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import healthy from "./fixtures/healthy.json";
import truncated from "./fixtures/truncated.json";
import missingContentTypes from "./fixtures/missing-content-types.json";
import unclosedTags from "./fixtures/unclosed-tags.json";
import badCharacters from "./fixtures/bad-characters.json";
import missingDocumentPart from "./fixtures/missing-document-part.json";
import notAWordFile from "./fixtures/not-a-word-file.json";
import emptyBody from "./fixtures/empty-body.json";
import illustrated from "./fixtures/illustrated.json";
import truncatedIllustrated from "./fixtures/truncated-illustrated.json";

export interface Capture {
  name: string;
  filename: string;
  events: { stage: string; message: string }[];
  report: Record<string, unknown>;
}

export const CAPTURES: Capture[] = [
  healthy,
  truncated,
  missingContentTypes,
  unclosedTags,
  badCharacters,
  missingDocumentPart,
  notAWordFile,
  emptyBody,
  illustrated,
  truncatedIllustrated,
] as Capture[];

let serving: Capture = truncated as Capture;
export function serve(name: string) {
  const found = CAPTURES.find((c) => c.name === name);
  if (!found) throw new Error(`no captured repair called ${name}`);
  serving = found;
}

/** Cut the stream off after `n` lines, the way a dropped connection does. */
let cutAfter: number | null = null;
export function dropConnectionAfter(n: number | null) {
  cutAfter = n;
}

/** What this copy of the page can do. The default is off, because the offline
 *  rebuild is the product and styling is the extra. */
let styling: { on: boolean; note: string } = { on: false, note: "" };
export function serveCapabilities(on: boolean, note = "") {
  styling = { on, note };
}

/** How the styling endpoint answers next. Stages first, then the final line —
 *  the same wire shape as a repair, because it is the same situation. */
let stylingAnswer: {
  status: number;
  detail?: string;
  stages: string[];
  final: Record<string, unknown>;
} = {
  status: 200,
  stages: ["Sending the recovered content to SuperDocs for styling…"],
  final: {
    ok: true,
    notes: ["SuperDocs returned a styled file."],
    filename: "d-repaired-styled.docx",
    download: "/api/download/styled-token",
    ops_charged: 1,
    ops_confirmed: false,
    allowance_known: true,
    allowance_remaining: 42,
    warnings: 0,
  },
};
export function serveStyling(next: Partial<typeof stylingAnswer>) {
  stylingAnswer = { ...stylingAnswer, ...next };
}

function ndjson(lines: string[]) {
  const body = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      const limit = cutAfter ?? lines.length;
      lines.slice(0, limit).forEach((l) => controller.enqueue(encoder.encode(l + "\n")));
      controller.close();
    },
  });
  return new HttpResponse(body, { headers: { "content-type": "application/x-ndjson" } });
}

export const handlers = [
  http.get("/api/capabilities", () =>
    HttpResponse.json({ styling: styling.on, note: styling.note }),
  ),

  http.post("/api/style/:token", () => {
    if (stylingAnswer.status !== 200) {
      return HttpResponse.json({ detail: stylingAnswer.detail }, { status: stylingAnswer.status });
    }
    return ndjson([
      ...stylingAnswer.stages.map((m) => JSON.stringify({ stage: "superdocs", message: m })),
      JSON.stringify({ done: true, ...stylingAnswer.final }),
    ]);
  }),

  http.post("/api/repair", () => {
    const capture = serving;
    return ndjson([
      ...capture.events.map((e) => JSON.stringify(e)),
      JSON.stringify({ done: true, ...capture.report }),
    ]);
  }),
];

export const server = setupServer(...handlers);
