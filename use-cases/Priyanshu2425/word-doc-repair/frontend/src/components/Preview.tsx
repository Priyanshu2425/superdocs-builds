/**
 * What is actually in the file, before anybody decides whether it was worth it.
 *
 * From the research, on a paid repair site: *"It ran for a bit and then wanted
 * forty dollars to download the result... It just said 'repair successful'. I
 * didn't know if it had actually got anything."* They did not pay, and they
 * rewrote the document by hand over a Sunday.
 *
 * A verdict is a claim. This is the thing itself — and it is the one part of
 * this page whose honesty needs no wording at all.
 *
 * The markup comes from the engine, which escapes every piece of text it is
 * given and emits no attribute it did not construct, so what lands here is the
 * reader's own document and nothing else.
 */
export function Preview({ html }: { html: string }) {
  if (!html.trim()) return null;
  return (
    <section className="preview" aria-labelledby="preview-h">
      <h3 className="outcome__head" id="preview-h">
        What is in the file
      </h3>
      <p className="preview__note">
        Read it here before you download it. This is the rebuilt document itself, not a
        description of it.
      </p>
      <div className="preview__sheet" tabIndex={0} role="group" aria-label="The rebuilt document">
        <div className="preview__doc" dangerouslySetInnerHTML={{ __html: html }} />
      </div>
    </section>
  );
}
