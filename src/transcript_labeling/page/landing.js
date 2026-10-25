// The list of projects and datasets, fetched with the browser session.
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const session = (() => { try { return localStorage.getItem("tl-session") || ""; } catch (e) { return ""; } })();
const box = document.getElementById("projects"), msg = document.getElementById("message");
const r = await fetch("/api/projects", {headers: {"X-TL-Session": session}});
if (!r.ok) {
 msg.hidden = false;
 msg.textContent = r.status === 401 ? "This browser is not logged in. Open a login link (tl url)." : "The server answered " + r.status + ".";
} else {
 const {projects} = await r.json();
 box.innerHTML = projects.length ? projects.map(p => `<div class="mode"><h3 class="mode-hd">${esc(p.title)}<small>${esc(p.name)} · ${esc(p.privacy)}</small></h3>
  ${p.datasets.map(d => d.error ? `<p class="status err">${esc(d.name)}: ${esc(d.error)}</p>`
   : `<p><a class="yt" href="/p/${encodeURIComponent(p.name)}/${encodeURIComponent(d.name)}/">${esc(d.title)}</a>
      <span class="status">${d.items} items · ${d.with_ranges} with speaker marks · ${d.done} done${d.finished ? " · finished" : ""}</span></p>`).join("")}</div>`).join("")
  : '<p class="sub">No projects yet. An agent creates one with tl new.</p>';
}
