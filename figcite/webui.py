"""The whole front end: one HTML string, no build step, no assets.

Kept in Python rather than a data file so packaging needs no new
package-data entry and the server does no filesystem lookup per request.
"""

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>figcite</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#111; --mut:#666;
          --line:#8a8a8a; --warn:#a40000; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151515; --fg:#eee; --mut:#999; --line:#6b6b6b; --warn:#ff8a80; }
  }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif;
         margin:0; padding:1.5rem; }
  nav button { font:inherit; padding:.4rem .9rem; border:1px solid var(--line);
               background:transparent; color:inherit; cursor:pointer; }
  nav button[aria-selected=true] { border-bottom:2px solid var(--fg); font-weight:600; }
  .card { display:flex; gap:1rem; border:1px solid var(--line); padding:1rem;
          margin:1rem 0; align-items:flex-start; }
  .card img { max-width:260px; max-height:260px; border:1px solid var(--line); }
  .info { min-width:0; }
  .ctx { color:var(--mut); }
  .note { color:var(--mut); }
  .fail { color:var(--warn); font-weight:600; }
  .ev { color:var(--mut); font-style:italic; }
  label { display:block; margin:.2rem 0; }
  .src { display:inline-block; font-size:.75em; text-transform:uppercase;
         letter-spacing:.03em; padding:.05rem .4rem; margin-right:.4em;
         border:1px solid var(--line); border-radius:.25rem; color:var(--mut); }
  .badge { display:inline-block; padding:0 .4rem; border:1px solid var(--line);
           border-radius:3px; font-size:12px; }
  .badge.warn { color:var(--warn); border-color:var(--warn); }
  .retracted { color:var(--warn); font-weight:700; }
  table { border-collapse:collapse; width:100%; }
  td, th { border-bottom:1px solid var(--line); padding:.4rem; text-align:left;
           vertical-align:top; }
</style>
<nav>
  <button id="tab-pending" data-tab="pending" aria-selected="true">Pending</button>
  <button id="tab-deck" data-tab="deck" aria-selected="false">Deck</button>
</nav>
<section id="pending"></section>
<section id="deck" hidden>
  <p>
    <input id="deckpath" size="60" placeholder="/path/to/deck.pptx">
    <button id="deck-audit-btn" type="button">Audit</button>
  </p>
  <p>
    <input id="deckout" size="60" placeholder="output path (default: &lt;name&gt;.cited.ext)">
    <label><input type="checkbox" id="deckforce"> overwrite if it already exists</label>
    <button id="deck-apply-btn" type="button">Apply</button>
  </p>
  <p id="decksummary"></p>
  <table id="deckrows"></table>
</section>
<script>
const $ = (s) => document.querySelector(s);

function show(which) {
  for (const t of ["pending", "deck"]) {
    $("#" + t).hidden = (t !== which);
    $("#tab-" + t).setAttribute("aria-selected", String(t === which));
  }
}

// Review fold-in: these two tab buttons used to carry a static inline
// click-handler attribute literal (harmless on its own -- no interpolated
// value ever reached it -- but every OTHER interactive element in this page
// goes through delegation, and this makes that actually true instead of
// true-except-here).
document.querySelector("nav").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (tab) show(tab);
});

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// Shared by loadPending() and post(): a non-2xx response from this server is
// always JSON (see web.py's _json()), but a request that never reaches our
// routing -- a malformed request the stdlib itself rejects -- can come back
// as an HTML error page that r.json() cannot parse. Falling back to the raw
// status keeps that case from throwing before the failure is ever shown.
async function errMsg(r) {
  let msg = `${r.status} ${r.statusText}`;
  try {
    const body = await r.json();
    if (body && body.error) msg = body.error;
  } catch {}
  return msg;
}

async function loadPending() {
  const el = $("#pending");
  try {
    const r = await fetch("/api/pending");
    if (!r.ok) throw new Error(await errMsg(r));
    const { items } = await r.json();
    el.innerHTML = (items && items.length)
      ? items.map(card).join("")
      : "<p>nothing pending. Run <code>figcite watch</code>, then snip something.</p>";
  } catch (e) {
    // Rule 2's failure mode again, one level up: a broken loader must not
    // read as "there is nothing pending" -- that's the same lie the
    // per-item LOOKUP FAILED banner exists to prevent, at the list level
    // instead of the item level. Without this, an un-awaited throw here
    // leaves the section blank forever, indistinguishable from empty.
    el.innerHTML = `<p class="fail">COULD NOT LOAD PENDING ITEMS: ${esc(e.message || String(e))}</p>`;
  }
}

function card(item) {
  let body;
  let prefillDoi = "";
  // Controller Ruling 5: the error is a BANNER, not a branch. A failure in one
  // lookup does not invalidate candidates another lookup returned, and hiding
  // usable candidates behind a failure notice would discard real information.
  const banner = item.error
    ? `<p class="fail">LOOKUP FAILED: ${esc(item.error)}</p>`
    : "";
  if (item.error && !item.candidates.length && !item.doi) {
    // A failed lookup and a genuine no-match must never read the same.
    body = `<p>Enter a DOI by hand below.</p>`;
  } else if (item.doi && item.grounded) {
    body = `<p><strong>${esc(item.doi)}</strong>
              <span class="ev">evidence: ${esc(item.doi_evidence)}</span></p>`;
  } else if (item.doi) {
    // grounded === false but a DOI exists: figcite has an unverified
    // SOMETHING, not nothing. Rendering "no source inferred" here would be
    // rule 1's defect, inverted -- claiming absence where there is an
    // unconfirmed guess (this is the normal output of a nearest-visit
    // Firefox match, whose own evidence string says "confirm before
    // citing"). Show it as unconfirmed, pre-fill the box so the reviewer
    // sees exactly what a Confirm click will send, but never submit it
    // without that explicit click.
    body = `<p><strong>${esc(item.doi)}</strong> <span class="fail">(unconfirmed)</span>
              <span class="ev">evidence: ${esc(item.doi_evidence)}</span></p>`;
    prefillDoi = item.doi;
  } else if (item.candidates.length) {
    body = item.candidates.map((c, i) => `
      <label><input type="radio" name="pick" class="pick" value="${i}">
        <span class="src">${esc(c.source || "")}</span> ${esc(c.score || "")} &mdash;
        ${esc(c.title)} (${esc(c.container || "")} ${esc(c.year || "")})
      </label>`).join("");
  } else {
    body = `<p class="ctx">no source inferred${
      item.doi_evidence ? ": " + esc(item.doi_evidence) : ""}</p>`;
  }
  // A grounded DOI needs no radio and no typed text: confirmRef() sends
  // {ref} alone, and service.confirm() resolves zero selectors to "use this
  // item's own grounded DOI" -- the NotGrounded check is what refuses to
  // let that same path accept an ungrounded guess, so the safety lives
  // server-side, where it belongs, not in whether this button is enabled.
  const canConfirm = (item.doi && item.grounded) || !!prefillDoi;
  return `<form class="card" data-ref="${esc(item.ref)}"
                data-grounded="${item.doi && item.grounded ? "1" : "0"}"
                onsubmit="return false">
    <img src="/api/thumb?ref=${encodeURIComponent(item.ref)}" alt="">
    <div class="info">
      <p class="ctx">${esc(item.context)}</p>
      ${item.note ? `<p class="note">${esc(item.note)}</p>` : ""}
      ${banner}
      ${body}
      <p><input class="doi-input" placeholder="10.xxxx/yyyy" value="${esc(prefillDoi)}"></p>
      <p>
        <button class="ok-btn" type="button" ${canConfirm ? "" : "disabled"}>Confirm</button>
        ${item.kind !== "filed"
          ? '<button class="own-work-btn" type="button">This is my own work</button>' : ""}
        <button class="skip-btn" type="button">Skip</button>
      </p>
    </div></form>`;
}

function updateConfirm(cardEl) {
  const typed = cardEl.querySelector(".doi-input").value.trim();
  const picked = cardEl.querySelector(".pick:checked");
  const grounded = cardEl.dataset.grounded === "1";
  cardEl.querySelector(".ok-btn").disabled = !(typed || picked || grounded);
}

async function post(url, payload) {
  const r = await fetch(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
  if (!r.ok) {
    const msg = await errMsg(r);
    alert(msg);
    throw new Error(msg);
  }
  return r.json();
}

async function confirmRef(cardEl) {
  const ref = cardEl.dataset.ref;
  const typed = cardEl.querySelector(".doi-input").value.trim();
  const picked = cardEl.querySelector(".pick:checked");
  const grounded = cardEl.dataset.grounded === "1";
  let body;
  if (typed) body = {ref, doi: typed};
  else if (picked) body = {ref, pick: Number(picked.value)};
  else if (grounded) body = {ref};
  else return; // Confirm should not be reachable in this state.
  await post("/api/confirm", body);
  loadPending();
}

// Delegated on the section, not per-card: card() rebuilds #pending's
// innerHTML on every loadPending(), and a listener attached to an element
// that gets thrown away would silently stop firing. Using `dataset.ref`
// (read off the DOM, HTML-entity-decoded by the parser) instead of
// interpolating ref into a JS string literal means no ref value -- however
// it's spelled -- can ever break out of a quoted attribute.
const pendingSection = $("#pending");

pendingSection.addEventListener("input", (e) => {
  const cardEl = e.target.closest("[data-ref]");
  if (!cardEl || !e.target.classList.contains("doi-input")) return;
  // Typing a DOI and a picked radio must never disagree about what gets
  // sent -- clear the radio the instant the box gets text, so the card
  // never shows one decision while sending another.
  const picked = cardEl.querySelector(".pick:checked");
  if (picked) picked.checked = false;
  updateConfirm(cardEl);
});

pendingSection.addEventListener("change", (e) => {
  const cardEl = e.target.closest("[data-ref]");
  if (!cardEl || !e.target.classList.contains("pick")) return;
  cardEl.querySelector(".doi-input").value = "";
  updateConfirm(cardEl);
});

pendingSection.addEventListener("click", async (e) => {
  const cardEl = e.target.closest("[data-ref]");
  if (!cardEl) return;
  const ref = cardEl.dataset.ref;
  if (e.target.classList.contains("ok-btn")) {
    await confirmRef(cardEl);
  } else if (e.target.classList.contains("own-work-btn")) {
    await post("/api/confirm", {ref, own_work: true});
    loadPending();
  } else if (e.target.classList.contains("skip-btn")) {
    await post("/api/skip", {ref});
    loadPending();
  }
});

loadPending();

// --- Deck screen -----------------------------------------------------------

// Friendly labels for the seven verdicts crossref.classify_reuse can
// return. crossref.py's REUSE_VERDICTS frozenset is the actual source of
// truth for what those seven are -- tests/test_webui_deck.py reads that
// constant directly, not this map or classify_reuse's source text, so a
// verdict added there is checked against a real shared value, not a
// re-parsed guess. This map only supplies nicer wording; badgeFor()'s
// fallback below is what makes an unmapped verdict safe to skip here.
const REUSE = {
  "public-domain": ["public domain", ""],
  "reuse-ok-attribution-required": ["CC-BY: cite it", ""],
  "reuse-ok-share-alike-attribution-required": [
    "CC-BY-SA: cite + share alike",
    "",
  ],
  "noncommercial-only": ["noncommercial only", "warn"],
  "restricted-no-derivatives": ["no derivatives", "warn"],
  "publisher-terms-check-required": ["check publisher terms", "warn"],
  "unknown-ask-publisher": ["license unknown: ask", "warn"],
  // Not a classify_reuse verdict -- provenance.Record.reuse's own dataclass
  // default (see provenance.py), for a record built without ever calling
  // classify_reuse at all (an own-work confirmation, a clipboard/matplotlib
  // capture, anything with no DOI). Confirmed live against the demo deck:
  // every one of its "This work" rows carries this exact string. This is
  // now a *friendlier label*, not the only thing standing between this
  // string and a blank badge -- badgeFor()'s fallback covers that even if
  // this entry is deleted.
  unknown: ["no licence recorded", "warn"],
};

function badgeFor(r) {
  // Own work has no third-party licence to ask about, and a warn-colored
  // badge on every one of a user's own figures trains a user to ignore
  // red -- measured live on this project's own demo deck, where all 15
  // rows are own work and would otherwise show 15 red badges reading
  // "no licence recorded". Neutral styling; bypasses the reuse map.
  if (r.source_kind === "generated") return ["own work", ""];
  // Review fix C2: a verdict this map has never heard of -- a genuinely
  // new classify_reuse verdict before anyone gets around to adding a
  // friendly label, or any other truthy string -- renders AS ITSELF, in
  // warn styling, instead of ["", ""]. A blank badge for a truthy
  // r.reuse is impossible by construction now: there is no path left
  // that can produce an empty label.
  return REUSE[r.reuse] || [r.reuse, "warn"];
}

function deckRow(r) {
  const [text, cls] = badgeFor(r);
  const badge = r.reuse ? `<span class="badge ${cls}">${esc(text)}</span>` : "";
  const flag = r.retracted ? ' <span class="retracted">RETRACTED</span>' : "";
  // decorative rows are excluded from the headline untagged_substantive
  // count (figcite/deck.py) -- marked here too, so a deck with a pile of
  // tiny bullet icons never shows "0 substantive but unsourced" above a
  // table of no-source rows that look identical to the substantive kind.
  const decorative = r.decorative ? ' <span class="ctx">(decorative)</span>' : "";
  // A row's ref can be empty when nothing matched -- an empty ref is not a
  // thumbnail request the /api/thumb route can answer (it 404s "unknown
  // ref"), so skip the <img> entirely rather than send one. A non-empty
  // but unresolvable ref (a record present in the audit but absent from
  // the library manifest, reproduced live with an EXIF-credit JPEG) still
  // 404s at request time -- the capture-phase "error" listener below turns
  // that into a legible placeholder instead of a broken-image icon.
  const thumb = r.ref
    ? `<img src="/api/thumb?ref=${encodeURIComponent(r.ref)}" alt="" height="80">`
    : "";
  return `<tr><td>${esc(r.location)}<td>${thumb}
    <td>${esc(r.status)}${decorative} <span class="ctx">[${esc(r.matched_by)}]</span>
    <td>${esc(r.citation)}<td>${badge}${flag}</tr>`;
}

// <img> "error" events do not bubble, so this listener must run in the
// capture phase to see one at all. Attached once, to the <table> element
// itself -- auditDeck() only ever rewrites the table's innerHTML (its
// children), never the table element, so this survives every re-render
// without needing to be re-attached per row.
$("#deckrows").addEventListener(
  "error",
  (e) => {
    if (e.target.tagName !== "IMG") return;
    const span = document.createElement("span");
    span.className = "ctx";
    span.textContent = "(image unavailable)";
    e.target.replaceWith(span);
  },
  true,
);

async function auditDeck() {
  const path = $("#deckpath").value.trim();
  $("#decksummary").textContent = "auditing…";
  $("#deckrows").innerHTML = "";
  let rep;
  try {
    rep = await post("/api/audit", { path });
  } catch (e) {
    // post() already alerted; a stale or blank summary after a failed
    // audit would be indistinguishable from "0 pictures, none unsourced"
    // -- the same lie loadPending()'s catch exists to prevent, here for
    // the headline number instead of the item list.
    $("#decksummary").innerHTML =
      `<span class="fail">COULD NOT AUDIT: ${esc(e.message || String(e))}</span>`;
    return;
  }
  $("#decksummary").textContent =
    `${rep.pictures} picture(s), ${rep.tagged} with provenance, ` +
    `${rep.unconfirmed} unconfirmed, ${rep.untagged_substantive} substantive but unsourced`;
  $("#deckrows").innerHTML =
    "<tr><th>where<th>figure<th>status<th>citation<th>licence</tr>" +
    rep.rows.map(deckRow).join("");
}

async function applyDeck() {
  const path = $("#deckpath").value.trim();
  const out = $("#deckout").value.trim();
  const force = $("#deckforce").checked;
  let result;
  try {
    // `out` empty string -> service.apply()'s own `out or _default_out(path)`
    // fallback picks "<name>.cited.<ext>", same as leaving the field out
    // entirely -- sending it unconditionally keeps this payload static
    // rather than conditionally shaped.
    result = await post("/api/apply", { path, out, force });
  } catch (e) {
    // post() already alerted with the server's real reason (an existing
    // out without force=, a mismatched suffix); returning here is what
    // stops that rejection from also surfacing as an unhandled one in the
    // delegated click listener below -- auditDeck() has the equivalent
    // try/catch above, this one was missing it.
    return;
  }
  alert("wrote " + (result.out || result.path || "the cited deck"));
}

// Delegated on the section, matching #pending: the buttons carry no
// interpolated value, but routing clicks through one listener instead of
// per-button `onclick="fn()"` keeps every interactive element on this
// screen off the string-built-onclick shape that made a ref-scheme change
// one step from XSS on the pending screen, so a later edit that DOES need
// to interpolate a ref has nowhere on this screen to reach for it.
$("#deck").addEventListener("click", async (e) => {
  if (e.target.id === "deck-audit-btn") await auditDeck();
  else if (e.target.id === "deck-apply-btn") await applyDeck();
});
</script>
"""
