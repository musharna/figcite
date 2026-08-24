// Behavioural checks for the JavaScript inside figcite/webui.py.
//
// webui.py is 596 lines of which 99.9% sit inside ONE string literal, so the
// Python mutation sweep generated exactly zero mutants for it: there is no
// Python there to mutate. The file nonetheless holds 16 named functions, 30
// `if` statements and 13 comparisons -- a real program, and the half of the
// tool a user actually looks at.
//
// The existing Python tests assert SUBSTRINGS of that string ("LOOKUP FAILED"
// in PAGE). That checks the text exists somewhere; it cannot check which
// branch produces it, or that the other branch does not.
//
// This file is concatenated after the extracted <script> by
// test_webui_behaviour.py and run under node, so every function below is the
// real one, taken from the real PAGE at test time. It cannot drift.

let failures = 0;
function check(name, cond, detail) {
  if (!cond) {
    failures++;
    console.error(`FAIL ${name}${detail ? ": " + detail : ""}`);
  }
}

// ---------------------------------------------------------------- esc

check("esc escapes angle brackets",
  esc("<script>alert(1)</script>") === "&lt;script&gt;alert(1)&lt;/script&gt;",
  esc("<script>alert(1)</script>"));
check("esc escapes quotes and ampersands",
  esc('a & "b"') === "a &amp; &quot;b&quot;", esc('a & "b"'));
check("esc renders null as empty, not the word null",
  esc(null) === "" && esc(undefined) === "", esc(null));
// A citation is attacker-influenced text: it comes from CrossRef, from a
// window title, from a filename. Rendering it unescaped is the one way this
// page can execute someone else's markup.
check("a hostile citation cannot inject markup",
  !confirmedHtml({citation: '<img src=x onerror=alert(1)>', path: "p"})
    .includes("<img src=x"),
  confirmedHtml({citation: '<img src=x onerror=alert(1)>', path: "p"}));

// ---------------------------------------------------------------- badgeFor

check("own work gets a neutral badge",
  JSON.stringify(badgeFor({source_kind: "generated", reuse: "anything"}))
    === JSON.stringify(["own work", ""]));
check("a known verdict gets its friendly label",
  badgeFor({reuse: "reuse-ok-attribution-required"})[0] === "CC-BY: cite it");
check("a restricted verdict is warn-styled",
  badgeFor({reuse: "noncommercial-only"})[1] === "warn");
// The C2 review fix: an unrecognised verdict must render AS ITSELF rather
// than as a blank badge, or a new classify_reuse verdict silently shows the
// user nothing at all.
const unknown = badgeFor({reuse: "some-brand-new-verdict"});
check("an unknown verdict renders as itself in warn styling",
  unknown[0] === "some-brand-new-verdict" && unknown[1] === "warn",
  JSON.stringify(unknown));
check("no truthy reuse can produce a blank label",
  badgeFor({reuse: "x"})[0] !== "", JSON.stringify(badgeFor({reuse: "x"})));

// ---------------------------------------------------------------- confirmedHtml

const retracted = confirmedHtml({retracted: true, citation: "A paper", path: "p"});
check("a retracted work gets an alert banner", retracted.includes('role="alert"'));
check("the retraction banner comes FIRST",
  retracted.indexOf("RETRACTED") < retracted.indexOf("confirmed:"),
  retracted.slice(0, 120));
check("a normal confirm has no retraction banner",
  !confirmedHtml({citation: "A paper", path: "p"}).includes("RETRACTED"));
check("a filed ref with no path says the file is unchanged",
  confirmedHtml({citation: "A paper"}).includes("unchanged"),
  confirmedHtml({citation: "A paper"}));
check("a confirm WITH a path names the destination",
  confirmedHtml({citation: "A paper", path: "lib/fig.png"}).includes("lib/fig.png"));

// ---------------------------------------------------------------- whereisHtml

const match = whereisHtml({verdict: "match", matches: [
  {evidence: true, source: "dhash", doi: "10.1/a", score: 0, score_label: "hamming 0"}]});
const nomatch = whereisHtml({verdict: "no-match", matches: []});
const cnd = whereisHtml({verdict: "could-not-decide", reason: "opencv missing",
  matches: []});

check("a match says it was found", match.includes("found in your corpus"));
check("no-match says the corpus WAS searched",
  nomatch.includes("searched") && nomatch.includes("not in your corpus"), nomatch);
check("could-not-decide is not worded like no-match",
  cnd.includes("COULD NOT DECIDE") && !cnd.includes("not in your corpus"), cnd);
check("could-not-decide says nothing was ruled out",
  cnd.includes("nothing was ruled out"), cnd);
// The three verdicts must be mutually distinguishable, not merely non-empty.
check("the three verdicts render differently",
  match !== nomatch && nomatch !== cnd && match !== cnd);
check("could-not-decide carries its reason", cnd.includes("opencv missing"), cnd);

// A dhash score of 0 is the STRONGEST possible result. `||` would blank it.
check("a zero score renders as 0, not blank",
  whereisHtml({verdict: "match", matches: [
    {evidence: true, source: "dhash", doi: "10.1/a", score: 0}]}).includes(">0<"),
  whereisHtml({verdict: "match", matches: [
    {evidence: true, source: "dhash", doi: "10.1/a", score: 0}]}));

// Leads are never evidence.
const withLeads = whereisHtml({verdict: "no-match", matches: [
  {evidence: false, source: "open-tab", doi: "10.1/tab", title: "T"}]});
check("open-tab leads are labelled as never evidence",
  withLeads.includes("never evidence"), withLeads);
check("a lead alone does not print the accept-one instruction",
  !withLeads.includes("figcite confirm"), withLeads);
check("evidence DOES print the accept-one instruction",
  match.includes("figcite confirm"), match);

// ---------------------------------------------------------------- card

const errOnly = card({ref: "r", error: "429 throttled", candidates: [], doi: null});
check("a failed lookup shows the failure", errOnly.includes("LOOKUP FAILED"));
check("a failed lookup with nothing else asks for a DOI by hand",
  errOnly.includes("Enter a DOI by hand"), errOnly);
check("a failed lookup does NOT read like a genuine no-match",
  !errOnly.includes("no source inferred"), errOnly);

// Controller Ruling 5: the error is a BANNER, not a branch -- candidates found
// by another route survive a failure in this one.
const errWithCands = card({ref: "r", error: "429 throttled", doi: null,
  candidates: [{source: "crossref", title: "A paper", container: "J", year: "2020"}]});
check("candidates survive alongside a lookup failure",
  errWithCands.includes("LOOKUP FAILED") && errWithCands.includes("A paper"),
  errWithCands);

const grounded = card({ref: "r", doi: "10.1/g", grounded: true,
  doi_evidence: "exact title match", candidates: []});
check("a grounded DOI shows its evidence", grounded.includes("exact title match"));
check("a grounded DOI is marked confirmable",
  grounded.includes('data-grounded="1"'), grounded.slice(0, 200));

const guess = card({ref: "r", doi: "10.1/u", grounded: false,
  doi_evidence: "nearest visit", candidates: []});
check("an ungrounded DOI is shown as unconfirmed",
  guess.includes("(unconfirmed)"), guess);
check("an ungrounded DOI is NOT marked grounded",
  guess.includes('data-grounded="0"'), guess.slice(0, 200));
check("an ungrounded guess is not reported as no source",
  !guess.includes("no source inferred"), guess);

const nothing = card({ref: "r", doi: null, candidates: [], doi_evidence: "why not"});
check("nothing found says so, with the reason",
  nothing.includes("no source inferred") && nothing.includes("why not"), nothing);

// No candidate is ever pre-selected -- the same guarantee the Python test
// asserts over the page source, asserted here over rendered output.
const picks = card({ref: "r", doi: null, candidates: [
  {source: "crossref", title: "A"}, {source: "zotero", title: "B"}]});
check("no candidate radio is pre-checked",
  !/<input[^>]*\bchecked\b/.test(picks), picks);

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("all webui behaviour checks passed");
