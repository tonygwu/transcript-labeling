// The page is an empty shell. This prelude fetches its data from the server with the
// browser session the login link gave it, and sends that session on every request.
// Everything that follows runs in one module, so this `fetch` is the only one the
// page's code sees.
const TL_SESSION_KEY = "tl-session";
function tlSession(){ try { return localStorage.getItem(TL_SESSION_KEY) || ""; } catch (e) { return ""; } }
const fetch = (url, opt = {}) => window.fetch(url, {...opt, headers: {...(opt.headers || {}), "X-TL-Session": tlSession()}});
function tlStop(message){
 const b = document.getElementById("banner"); b.hidden = false; b.textContent = message;
 throw new Error(message);
}
async function tlLoadPage(){
 let res;
 try { res = await fetch("api/page", {headers: {accept: "application/json"}}); }
 catch (e) { tlStop("The labeling server is not answering. Is it running? (tl doctor)"); }
 if (res.status === 401) tlStop("This browser is not logged in, or its login expired. Open a new login link (tl url).");
 if (!res.ok) tlStop("The server could not load this dataset (" + res.status + ").");
 const data = await res.json();
 document.title = data.session.title;
 document.getElementById("title").textContent = data.session.title;
 document.getElementById("subtitle").textContent =
  "Modes: " + data.session.modes.join(", ") + ". Each answer saves the moment you make it.";
 return data;
}
