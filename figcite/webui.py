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
  <button id="tab-pending" aria-selected="true" onclick="show('pending')">Pending</button>
  <button id="tab-deck" aria-selected="false" onclick="show('deck')">Deck</button>
</nav>
<section id="pending"></section>
<section id="deck" hidden>
  <p>
    <input id="deckpath" size="60" placeholder="/path/to/deck.pptx">
    <button id="deck-audit-btn" type="button">Audit</button>
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

// Every verdict crossref.classify_reuse can return. A verdict missing from
// this map would render blank, and blank reads as "fine" -- the opposite of
// what an unknown or restricted license means. tests/test_webui_deck.py
// derives the verdict set from classify_reuse's own source (not a copy of
// this list) so a verdict added there tomorrow fails that test until a
// badge for it exists here too.
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
  // Not a classify_reuse verdict -- provenance.Record.reuse's own default
  // (see provenance.py), for a record built without ever calling
  // classify_reuse at all (an own-work confirmation, a clipboard/matplotlib
  // capture, anything with no DOI). Confirmed live against the demo deck:
  // every one of its "This work" rows carries this exact string. Leaving it
  // out of this map would render those rows' badge as an empty box -- the
  // same "blank reads as fine" failure this map exists to prevent, just
  // reached from provenance.py's default instead of crossref.py's classifier.
  unknown: ["no licence recorded", "warn"],
};

function deckRow(r) {
  const [text, cls] = REUSE[r.reuse] || ["", ""];
  const badge = r.reuse ? `<span class="badge ${cls}">${esc(text)}</span>` : "";
  const flag = r.retracted ? ' <span class="retracted">RETRACTED</span>' : "";
  // A row's ref can be empty when nothing matched -- an empty ref is not a
  // thumbnail request the /api/thumb route can answer (it 404s "unknown
  // ref"), so skip the <img> entirely rather than send one.
  const thumb = r.ref
    ? `<img src="/api/thumb?ref=${encodeURIComponent(r.ref)}" alt="" height="80">`
    : "";
  return `<tr><td>${esc(r.location)}<td>${thumb}
    <td>${esc(r.status)} <span class="ctx">[${esc(r.matched_by)}]</span>
    <td>${esc(r.citation)}<td>${badge}${flag}</tr>`;
}

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
  // A rejected apply() (existing output without force=, mismatched out
  // suffix) is already surfaced by post()'s alert(); nothing below this
  // await runs in that case, so there is nothing further to swallow.
  const out = await post("/api/apply", { path: $("#deckpath").value.trim() });
  alert("wrote " + (out.out || out.path || "the cited deck"));
}

// Delegated on the section, matching #pending: the buttons are static (no
// ref/path is ever interpolated into an attribute), but routing clicks
// through one listener instead of per-button `onclick="fn()"` keeps every
// interactive element in this file going through the same dataset/delegation
// path, so a later edit that DOES need to interpolate a value has nowhere
// on this screen to reach for the string-built-onclick shape that made a
// ref-scheme change one step from XSS on the pending screen.
$("#deck").addEventListener("click", async (e) => {
  if (e.target.id === "deck-audit-btn") await auditDeck();
  else if (e.target.id === "deck-apply-btn") await applyDeck();
});
</script>
"""
