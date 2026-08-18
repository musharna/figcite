"""The whole front end: one HTML string, no build step, no assets.

Kept in Python rather than a data file so packaging needs no new
package-data entry and the server does no filesystem lookup per request.
"""

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>figcite</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#111; --mut:#666;
          --line:#ddd; --warn:#a40000; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151515; --fg:#eee; --mut:#999; --line:#333; --warn:#ff8a80; }
  }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif;
         margin:0; padding:1.5rem; }
  nav button { font:inherit; padding:.4rem .9rem; border:1px solid var(--line);
               background:transparent; color:inherit; cursor:pointer; }
  nav button[aria-selected=true] { border-bottom:2px solid var(--fg); font-weight:600; }
  .card { display:flex; gap:1rem; border:1px solid var(--line); padding:1rem;
          margin:1rem 0; align-items:flex-start; }
  .card img { max-width:260px; max-height:260px; border:1px solid var(--line); }
  .ctx { color:var(--mut); }
  .fail { color:var(--warn); font-weight:600; }
  .ev { color:var(--mut); font-style:italic; }
  label { display:block; margin:.2rem 0; }
  .src { display:inline-block; font-size:.75em; text-transform:uppercase;
         letter-spacing:.03em; padding:.05rem .4rem; margin-right:.4em;
         border:1px solid var(--line); border-radius:.25rem; color:var(--mut); }
</style>
<nav>
  <button id="tab-pending" aria-selected="true" onclick="show('pending')">Pending</button>
  <button id="tab-deck" aria-selected="false" onclick="show('deck')">Deck</button>
</nav>
<section id="pending"></section>
<section id="deck" hidden></section>
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

async function loadPending() {
  const r = await fetch("/api/pending");
  const { items } = await r.json();
  $("#pending").innerHTML = items.length
    ? items.map(card).join("")
    : "<p>nothing pending. Run <code>figcite watch</code>, then snip something.</p>";
}

function card(item) {
  let body;
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
  } else if (item.candidates.length) {
    body = item.candidates.map((c, i) => `
      <label><input type="radio" name="pick-${esc(item.ref)}" value="${i}"
                    onchange="enable('${esc(item.ref)}')">
        <span class="src">${esc(c.source || "")}</span> ${esc(c.score || "")} &mdash;
        ${esc(c.title)} (${esc(c.container || "")} ${esc(c.year || "")})
      </label>`).join("");
  } else {
    body = `<p class="ctx">no source inferred${
      item.doi_evidence ? ": " + esc(item.doi_evidence) : ""}</p>`;
  }
  return `<div class="card" data-ref="${esc(item.ref)}">
    <img src="/api/thumb?ref=${encodeURIComponent(item.ref)}" alt="">
    <div>
      <p class="ctx">${esc(item.context)}</p>
      ${banner}
      ${body}
      <p><input placeholder="10.xxxx/yyyy" id="doi-${esc(item.ref)}"
                oninput="enable('${esc(item.ref)}')"></p>
      <p>
        <button id="ok-${esc(item.ref)}" disabled
                onclick="confirmRef('${esc(item.ref)}')">Confirm</button>
        <button onclick="ownWork('${esc(item.ref)}')">This is my own work</button>
        <button onclick="post('/api/skip', {ref:'${esc(item.ref)}'}).then(loadPending)">Skip</button>
      </p>
    </div></div>`;
}

function enable(ref) {
  const picked = document.querySelector(`input[name="pick-${ref}"]:checked`);
  const typed = $("#doi-" + CSS.escape(ref)).value.trim();
  $("#ok-" + CSS.escape(ref)).disabled = !(picked || typed);
}

async function post(url, payload) {
  const r = await fetch(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
  const out = await r.json();
  if (!r.ok) { alert(out.error || "failed"); throw new Error(out.error); }
  return out;
}

async function confirmRef(ref) {
  const picked = document.querySelector(`input[name="pick-${ref}"]:checked`);
  const typed = $("#doi-" + CSS.escape(ref)).value.trim();
  const body = typed ? {ref, doi: typed} : {ref, pick: Number(picked.value)};
  await post("/api/confirm", body);
  loadPending();
}

async function ownWork(ref) {
  await post("/api/confirm", {ref, own_work: true});
  loadPending();
}

loadPending();
</script>
"""
