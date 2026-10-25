// The login link is /login#code=<code>. The code is in the fragment, so the browser
// never sends it in a request line or a Referer. It is posted once, here.
const KEY = "tl-session";
const msg = document.getElementById("message"), cont = document.getElementById("continue");
const say = text => { msg.hidden = false; msg.textContent = text; };
const code = new URLSearchParams(location.hash.slice(1)).get("code");
history.replaceState(null, "", "/login");
const stored = () => { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } };
async function sessionWorks(){
 if (!stored()) return false;
 const r = await fetch("/api/whoami", {headers: {"X-TL-Session": stored()}});
 return r.ok;
}
function storageWorks(){
 try { localStorage.setItem(KEY + "-probe", "1"); localStorage.removeItem(KEY + "-probe"); return true; }
 catch (e) { return false; }
}
async function main(){
 if (!code) { say("This link has no login code. Ask for a new one (tl url)."); return; }
 // Check before redeeming: a redeemed code is spent even if the session cannot be kept.
 if (!storageWorks()) { say("This browser will not store the login (a private window?). Open this same link in a normal window; it has not been used."); return; }
 const r = await fetch("/api/login", {method: "POST", headers: {"content-type": "application/json"},
                                      body: JSON.stringify({code})});
 const j = await r.json().catch(() => ({}));
 if (r.ok) {
  try { localStorage.setItem(KEY, j.session); }
  catch (e) { say("This browser would not store the login, and the link is now used. Ask for a new link (tl url) and open it in a normal window."); return; }
  location.replace(j.target);
  return;
 }
 if (await sessionWorks()) {
  // A used link with a working session still continues, but says so: if you did not
  // open this link before, someone else may have.
  say("This link was already used or has expired. You are still logged in on this browser. If you did not open "
      + "this link before, someone else may have; ask for a new link.");
  cont.href = j.target || "/"; cont.hidden = false;
  return;
 }
 say((j.error || "This link did not work") + ".");
}
main().catch(e => say("Login failed: " + e.message));
