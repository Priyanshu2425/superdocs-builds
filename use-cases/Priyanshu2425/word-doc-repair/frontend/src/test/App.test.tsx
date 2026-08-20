/**
 * Salvage, tested at its one seam: the HTTP boundary.
 *
 * The guards below run over what is on the screen rather than over the engine's
 * strings. That distinction is the whole point -- BUG-014 was fixed in the
 * engine and came straight back as BUG-020, and a page can introduce copy the
 * engine never produced.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, beforeEach } from "vitest";

import App from "../App";
import { CAPTURES, dropConnectionAfter, serve, serveCapabilities, serveStyling } from "./server";

/** Vocabulary that belongs to the engine and never to a person's screen. */
const JARGON = [
  "ZIP", "XML", "CRC", "zlib", "central directory", "ParseError", "TimeoutError",
  "Traceback", "stack trace", "NoneType", "utf-8", "b'", "0x",
];

/** Promises this build is forbidden to make. A denial is not a promise --
 *  "not a complete repair" is the sentence this build exists to say -- so a
 *  match only counts when nothing negates it just before. */
const OVERCLAIM = [
  "fully repaired", "fully restored", "completely repaired", "complete repair",
  "guaranteed", "guarantee", "perfect", "perfectly", "100%", "flawless",
  "as good as new", "everything was recovered", "nothing was lost",
];

function claimsWithoutNegation(text: string, phrase: string): boolean {
  const lower = text.toLowerCase();
  const needle = phrase.toLowerCase();
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, at + 1)) {
    const before = lower.slice(Math.max(0, at - 28), at);
    if (!/\b(not|never|no|cannot|can't|isn't|doesn't|without)\b[^.]*$/.test(before)) return true;
  }
  return false;
}

function file(name = "broken.docx", size = 4096) {
  const f = new File([new Uint8Array(size)], name, {
    type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  });
  Object.defineProperty(f, "size", { value: size });
  return f;
}

async function hand(user: ReturnType<typeof userEvent.setup>, f = file()) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(input, f);
}

/** Render the page and hand it one of the recorded repairs. */
async function repairing(name: string) {
  serve(name);
  const user = userEvent.setup();
  render(<App />);
  await hand(user);
  return user;
}

beforeEach(() => {
  dropConnectionAfter(null);
  serve("truncated");
  serveCapabilities(false, "");
  serveStyling({
    status: 200,
    stages: ["Sending the recovered content to SuperDocs for styling…"],
    final: {
      ok: true,
      rejected_for_content: false,
      notes: ["SuperDocs returned a styled file."],
      filename: "broken-repaired-styled.docx",
      download: "/api/download/styled-token",
      ops_charged: 1,
      ops_confirmed: false,
      allowance_known: true,
      allowance_remaining: 42,
      warnings: 0,
    },
  });
});

describe("the styled copy", () => {
  /** Set up a finished repair on a page that can style. */
  async function repaired() {
    serveCapabilities(true, "Styling is available on this page.");
    const user = await repairing("truncated");
    await screen.findByText(/what came through/i);
    return user;
  }

  it("offers nothing when this copy of the page cannot style", async () => {
    serveCapabilities(false, "Styling is switched off on this copy of the page.");
    await repairing("truncated");
    await screen.findByText(/what came through/i);
    expect(screen.queryByRole("button", { name: /send it for styling/i })).toBeNull();
    // and it says so, rather than leaving a person wondering what they missed
    expect(screen.getByText(/switched off on this copy/i)).toBeInTheDocument();
  });

  it("never offers a styled copy of a document it could not repair", async () => {
    serveCapabilities(true, "Styling is available on this page.");
    await repairing("missing-document-part");
    await screen.findAllByText(/could not be repaired/i);
    expect(screen.queryByRole("button", { name: /send it for styling/i })).toBeNull();
  });

  it("waits to be asked, and says the plain file is already theirs", async () => {
    await repaired();
    expect(screen.getByRole("button", { name: /send it for styling/i })).toBeInTheDocument();
    expect(screen.getByText(/already yours/i)).toBeInTheDocument();
    // nothing has been sent: the second download does not exist yet
    expect(screen.queryByRole("link", { name: /download the styled file/i })).toBeNull();
  });

  it("hands back a second file without taking away the first", async () => {
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));

    const styled = await screen.findByRole("link", { name: /download the styled file/i });
    expect(styled).toHaveAttribute("href", "/api/download/styled-token");
    // the plain rebuild is still on the page, still downloadable
    expect(screen.getByRole("link", { name: /download the repaired file/i })).toBeInTheDocument();
    expect(screen.getByText(/untouched and still yours/i)).toBeInTheDocument();
  });

  it("says what it cost, and marks an unconfirmed number as an estimate", async () => {
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));
    await screen.findByRole("link", { name: /download the styled file/i });
    expect(screen.getByText(/1 estimated operation/i)).toBeInTheDocument();
  });

  it("keeps the plain file when styling fails, and says so without blaming them", async () => {
    serveStyling({
      final: {
        ok: false,
        rejected_for_content: false,
        notes: ["Styling did not work. The rebuilt file above is unchanged and still yours."],
        filename: "broken-repaired-styled.docx",
        download: null,
        ops_charged: 0,
        ops_confirmed: false,
        allowance_known: false,
        allowance_remaining: 0,
        warnings: 0,
      },
    });
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));

    expect(await screen.findByText(/no styled copy/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /download the styled file/i })).toBeNull();
    expect(screen.getByRole("link", { name: /download the repaired file/i })).toBeInTheDocument();
  });

  it("says plainly when a styled copy came back rewritten and was thrown away", async () => {
    serveStyling({
      final: {
        ok: false,
        rejected_for_content: true,
        notes: [
          "The styled version came back with the wording changed, so it was thrown away rather than handed over. Your document should say what you wrote. The rebuilt file is unchanged and still yours.",
        ],
        filename: "broken-repaired-styled.docx",
        download: null,
        ops_charged: 1,
        ops_confirmed: false,
        allowance_known: true,
        allowance_remaining: 41,
        warnings: 0,
      },
    });
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/thrown away/i);
    expect((await screen.findAllByText(/wording changed/i)).length).toBe(2);
    // and nothing is offered for download but the file they already had
    expect(screen.queryByRole("link", { name: /download the styled file/i })).toBeNull();
    expect(screen.getByRole("link", { name: /download the repaired file/i })).toBeInTheDocument();
  });

  it("reports an exhausted allowance as a refusal to spend, not as a failure of theirs",
    async () => {
      serveStyling({
        final: {
          ok: false,
          rejected_for_content: false,
          notes: [
            "There is no styling allowance left this month, so nothing was sent and nothing was spent. The rebuilt file is unchanged and still yours.",
          ],
          filename: "broken-repaired-styled.docx",
          download: null,
          ops_charged: 0,
          ops_confirmed: false,
          allowance_known: true,
          allowance_remaining: 0,
          warnings: 0,
        },
      });
      const user = await repaired();
      await user.click(screen.getByRole("button", { name: /send it for styling/i }));
      // Twice: once on the page, once in the live region a screen reader hears.
      expect((await screen.findAllByText(/nothing was spent/i)).length).toBe(2);
    });

  it("surfaces a refusal from the server in the reader's words", async () => {
    serveStyling({
      status: 409,
      detail: "Styling is switched off on this copy of the page. The rebuilt file above is unchanged and still yours.",
    });
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));
    expect((await screen.findAllByText(/switched off on this copy/i)).length).toBe(2);
  });

  it("says a quiet wait is normal while it works", async () => {
    serveStyling({ stages: ["Waiting for SuperDocs to finish — large documents can take minutes."] });
    const user = await repaired();
    await user.click(screen.getByRole("button", { name: /send it for styling/i }));
    await waitFor(() =>
      expect(screen.getByText(/take minutes|quiet wait is normal/i)).toBeInTheDocument(),
    );
  });
});

describe("arriving", () => {
  it("says what this is and what it will not do", async () => {
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/will not open/i);
    expect(screen.getByText(/best-effort recovery/i)).toBeInTheDocument();
    expect(screen.getByText(/your original is never changed/i)).toBeInTheDocument();
    expect(screen.getByText(/no account/i)).toBeInTheDocument();
  });

  it("takes a file by choosing one, with no account and no install", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user);
    expect(await screen.findByText(/recovered in part|^recovered$/i)).toBeInTheDocument();
  });
});

describe("what it refuses before sending anything", () => {
  it("says plainly when the file is empty", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user, file("broken.docx", 0));
    expect(await screen.findByRole("alert")).toHaveTextContent(/empty/i);
    expect(screen.queryByText(/reading your document/i)).not.toBeInTheDocument();
  });

  it("says plainly when the file is too large", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user, file("huge.docx", 21 * 1024 * 1024));
    expect(await screen.findByRole("alert")).toHaveTextContent(/larger than 20 MB/i);
  });

  it("tells someone with a .doc what to do about it", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user, file("old.doc"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/save it as \.docx/i);
  });
});

describe("watching it work", () => {
  it("keeps every real step it took, and puts the answer above them", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user);

    // The verdict is what someone came for, so the steps fold away behind it.
    const steps = await screen.findByText(/what it did, step by step/i);
    await user.click(steps);
    const stations = screen.getByRole("list", { name: /what this page did/i });
    expect(within(stations).getAllByRole("listitem").length).toBeGreaterThan(3);
    expect(stations.textContent).toMatch(/Opening the file/i);
    // ...and they are the engine's own stages, not a decorative sequence.
    const captured = CAPTURES.find((c) => c.name === "truncated")!;
    expect(within(stations).getAllByRole("listitem").length).toBe(captured.events.length);
  });

  it("says the connection dropped rather than freezing, and that nothing changed", async () => {
    dropConnectionAfter(2);
    const user = userEvent.setup();
    render(<App />);
    await hand(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/connection dropped/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/original is exactly as it was/i);
  });
});

describe("the report", () => {
  it("names what came through and what did not, and hands the file back", async () => {
    serve("truncated");
    const user = userEvent.setup();
    render(<App />);
    await hand(user);

    expect(await screen.findByText(/what came through/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /download the repaired file/i });
    expect(link).toHaveAttribute("href", expect.stringContaining("/api/download/"));
    expect(screen.getByText(/more than once/i)).toBeInTheDocument();
    expect(screen.getByText(/thirty minutes/i)).toBeInTheDocument();
  });

  it("makes no claim about what came through when nothing did", async () => {
    // BUG-012. A total failure that still shows the heading is the exact
    // overclaim this build says it does not make.
    serve("not-a-word-file");
    const user = userEvent.setup();
    render(<App />);
    await hand(user);

    expect((await screen.findAllByText(/could not be repaired/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/what came through/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /download/i })).not.toBeInTheDocument();
    expect(screen.getByText(/what went wrong/i)).toBeInTheDocument();
    // and the page does not head a total failure with a line about what was read
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /could not be repaired/i,
    );
  });

  it("treats a valid file with nothing in it as a failure, not a quiet success", async () => {
    serve("empty-body");
    const user = userEvent.setup();
    render(<App />);
    await hand(user);
    expect((await screen.findAllByText(/could not be repaired/i)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("link", { name: /download/i })).not.toBeInTheDocument();
  });

  it("lets someone repair another document without reloading", async () => {
    const user = userEvent.setup();
    render(<App />);
    await hand(user);
    await screen.findByText(/what came through/i);
    await user.click(screen.getByRole("button", { name: /repair another document/i }));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/will not open/i);
  });
});

describe("seeing it before taking it", () => {
  it("shows the rebuilt document itself, above the download", async () => {
    await repairing("illustrated");
    const preview = await screen.findByRole("group", { name: /the rebuilt document/i });
    expect(within(preview).getByText("Site inspection")).toBeInTheDocument();
    expect(within(preview).getByText(/No further defects were observed/)).toBeInTheDocument();

    /* Above the download, because the question it answers — was any of this
       worth it — is one somebody asks before they act, not after. */
    const download = screen.getByRole("link", { name: /download the repaired file/i });
    expect(preview.compareDocumentPosition(download))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("shows the pictures, because the pictures are the part people lose", async () => {
    await repairing("illustrated");
    const preview = await screen.findByRole("group", { name: /the rebuilt document/i });
    const img = within(preview).getByRole("img");
    expect(img.getAttribute("src")).toMatch(/^data:image\/png;base64,/);
  });

  it("says the preview is the document rather than a description of it", async () => {
    await repairing("illustrated");
    expect(await screen.findByText(/not a description of it/i)).toBeInTheDocument();
  });

  it("shows no preview when nothing could be recovered", async () => {
    await repairing("missing-document-part");
    expect((await screen.findAllByText(/could not be repaired/i)).length)
      .toBeGreaterThan(0);
    expect(screen.queryByRole("group", { name: /the rebuilt document/i })).toBeNull();
  });
});

describe("the language, over every document this build is tested on", () => {
  it.each(CAPTURES.map((c) => c.name))("says nothing in the engine's words: %s", async (name) => {
    serve(name);
    const user = userEvent.setup();
    const { container } = render(<App />);
    await hand(user);
    await waitFor(() =>
      expect(container.textContent).toMatch(/recovered|could not be repaired/i),
    );

    const text = container.textContent ?? "";
    const found = JARGON.filter((j) => text.includes(j));
    expect(found, `the page showed engine vocabulary: ${found.join(", ")}`).toEqual([]);
  });

  it.each(CAPTURES.map((c) => c.name))("promises nothing it cannot do: %s", async (name) => {
    serve(name);
    const user = userEvent.setup();
    const { container } = render(<App />);
    await hand(user);
    await waitFor(() =>
      expect(container.textContent).toMatch(/recovered|could not be repaired/i),
    );

    const text = (container.textContent ?? "").toLowerCase();
    const found = OVERCLAIM.filter((w) => claimsWithoutNegation(text, w));
    expect(found, `the page overclaimed: ${found.join(", ")}`).toEqual([]);
  });
});

describe("everyone can use it", () => {
  it("has no automatically detectable accessibility violations, idle or reporting", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);

    let results = await axe.run(container, { rules: { region: { enabled: false } } });
    expect(results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    await hand(user);
    await screen.findByText(/what came through/i);
    results = await axe.run(container, { rules: { region: { enabled: false } } });
    expect(results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
  });
});
