const D = await tlLoadPage();
// What this page asks comes from its session, never from the study: the modes it
// enables, the projects that tag each recording, and the checklist spec if any.
const S = D.session;
const CHECK = S.modes.includes("checklist") ? S.checklist : null;
const FIELDS = CHECK ? CHECK.fields : [];
const HAS = mode => S.modes.includes(mode);
// A session with only the captions mode has no answer to complete: progress is the
// recordings with corrections, and no recording is ever reported done.
const CAPTIONS_ONLY = !CHECK && !HAS("speakers");
const projectsOf = r => r.projects || S.projects;
const projectOf = c => c.project || S.projects[0];
// Speakers come from the dataset. A label may be a template such as "{person}",
// filled from the item's catalog row. A session block without them is a server
// this page does not match: stop rather than guess the server's defaults.
if(!Array.isArray(S.speakers) || !S.unclear || typeof S.unclear.id!=="string" || typeof S.mixed_answer!=="string")
 tlStop("The server sent this page a dataset without its speakers, unclear entry or mixed answer. The server and page versions differ; run tl doctor.");
const SPEAKERS = S.speakers;
const UNCLEAR = S.unclear;
const MIXED = S.mixed_answer;
const SPEAKER_IDS = SPEAKERS.map(s=>s.id);
const SPEAKER_INDEX = Object.fromEntries(SPEAKERS.map((s,i)=>[s.id,i]));
// A field the item lacks stays visible as `{field}`, so a missing name is noticed.
const fillLabel = (label, r) => label.replace(/\{(\w+)\}/g, (m,f)=> (r && r[f]) || m);
function speakerLabel(id, r){
 const s = SPEAKERS.find(x=>x.id===id) || (id===UNCLEAR.id ? UNCLEAR : null);
 return s ? fillLabel(s.label, r) : id;}
// Each speaker draws in its own pattern: spk0 to spk5, then unclear. Never color alone.
const speakerClass = id => id===UNCLEAR.id ? "unclear" : id in SPEAKER_INDEX ? "spk"+Math.min(SPEAKER_INDEX[id],5) : id;
const SPEAKER_KEYS = Object.fromEntries([...SPEAKERS, UNCLEAR].map(s=>[String(s.key).toLowerCase(), s.id]));
// verbatim's configuration names the expected speaker "subject"; a quote it did not
// say is a misattribution. Other datasets expect no particular speaker.
const LEGACY_SUBJECT = SPEAKERS[0]?.id === "subject";
// A play link exists only when the dataset names a media URL and the item a video.
function mediaUrl(r, t){
 const m = S.media; if(!m || !r || !r.video_id) return null;
 return m.url.replaceAll("{video_id}", encodeURIComponent(r.video_id)).replaceAll("{t}", String(Math.max(0, Math.floor(t||0))));}
// review.json: notes, done marks, reveals and Finish, owned by the server.
let reviewState = {notes:{}, done:{}, reveals:{}, finished:null};
const esc = s => String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const el = id => document.getElementById(id);

// ONE item per recording. A recording may need its checklist, a set of quote
// attributions, or both — everything about it is answered on one screen, so a video
// is never opened twice.
const REC = {};
for(const r of D.rows) REC[r.key] = {...r, person:r.person||"", ask:!!CHECK, quotes:[]};
for(const r of (D.quote_rows||[])){
 const x = REC[r.key] ||= {...r, ask:false, hints:[], excerpts:[], description:"", mentions:null, person:r.person||""};
 x.quotes = r.quotes; if(!x.title) Object.assign(x, {title:r.title, channel:r.channel, upload:r.upload, video_id:r.video_id, duration:r.duration, forms:r.forms, person:r.person, role:r.role});
}
// With model drafts (build --drafts), recordings the model flagged come first, then
// the ones it was confident about, then recordings that only have quotes to check.
// Without drafts every recording is in one section, so the order is as it was.
const HAS_DRAFTS = D.rows.some(r=>"doubt" in r);
const SECTIONS = ["Check first", "Model was confident", "Quotes only"];
const SECTION_HELP = ["The model was unsure, found the subject absent or not the main speaker, or has no draft",
 "The model answered with high confidence. Its draft is a suggestion, never an answer", "No recording questions, only flagged quotes"];
const section = r => !HAS_DRAFTS ? 0 : !r.ask ? 2 : (r.doubt||[]).length ? 0 : 1;
const recs = Object.values(REC).sort((a,b)=>section(a)-section(b) || (a.person.split(" ").pop()+a.person+a.upload).localeCompare(b.person.split(" ").pop()+b.person+b.upload));
// A page with no recording-level questions is only quotes.
const QUOTE_ONLY = !D.rows.length;
let filter=CAPTIONS_ONLY?"all":"todo", projectFilter="all", cur=recs.length?recs[0].key:null, focusQ=0;
let labels={}, attrib={}, saver=null, writable=false;

const answered = (f, v) => f.type==="bool" ? typeof v==="boolean" : f.type==="enum" ? f.options.some(o=>o.value===v)
 : typeof v==="string" && v.trim()!=="";
// Count the questions, not the blocks: a fresh recording has three unanswered
// questions and used to report "1 left", which reads as one click away from done.
const askLeft = r => {if(!r.ask) return 0; const l=labels[r.key]||{};
 return FIELDS.filter(f=>f.required && !answered(f, l[f.name])).length;};
const askDone = r => askLeft(r)===0;
// Quote results count only where the session has the speakers mode that makes them.
const openQuotes = r => HAS("speakers") ? r.quotes.filter(c=>!(attrib[c.qid]||{}).answer) : [];
const quotesDone = r => !openQuotes(r).length &&
 (!r.quotes.length || !HAS("speakers") || (!spanPending[r.key] && !spanErrors[r.key]));
// An item with no checklist and no focus passage is done when the owner marks it done.
const complete = r => !CAPTIONS_ONLY && (r.ask || r.quotes.length ? askDone(r) && quotesDone(r) : !!reviewState.done[r.key]);
function recState(r){
 if(CAPTIONS_ONLY) return correctionAnswers[r.key]?.edits?.length ? "part" : "todo";
 if(complete(r)){
  const l=labels[r.key]||{};
  if(r.ask && !CHECK.accept_when_true.every(f=>l[f]===true)) return "rej";
  if(LEGACY_SUBJECT && r.quotes.some(c=>["other","both"].includes((attrib[c.qid]||{}).answer))) return "rej";
  return "done";}
 return (labels[r.key]||spanAnswers[r.key]?.ranges.length||r.quotes.some(c=>(attrib[c.qid]||{}).answer)) ? "part" : "todo";}
const outstanding = r => askLeft(r) + openQuotes(r).length;
// The model's draft stays hidden until every checklist question has an answer, so
// those answers are blind and calibration can use them. A label that already says
// the draft was shown keeps it on screen.
const draftOpen = r => !!r?.draft && (askDone(r) || !!(labels[r.key]||{}).draft_shown);

function fmtT(s){if(s==null)return "";const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+":"+String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0");}
function fmtDate(d){return d&&d.length===8?`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`:"unknown";}
function highlight(text, forms){let h=esc(text).replace(/&gt;&gt;/g,'<span class="turn">⟩⟩ </span>');
 const sorted=[...(forms||[])].sort((a,b)=>b.length-a.length).map(f=>f.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"));
 if(!sorted.length) return h; return h.replace(new RegExp("\\b("+sorted.join("|")+")\\b","gi"),"<mark>$1</mark>");}
const visible = () => recs.filter(r=> (projectFilter==="all" || projectsOf(r).includes(projectFilter))
 && (filter==="all" || (filter==="todo" ? !complete(r) : complete(r))));
const chips = ps => S.projects.length>1 ? ps.map(p=>` <span class="pchip">${esc(p)}</span>`).join("") : "";
const curRec = () => REC[cur];

function renderList(){
 const vis=visible(), groups={}, per={};
 // One group per person within each section, so a person's flagged recordings
 // are not pulled down into their confident ones.
 for(const r of vis)(groups[section(r)+"\u0000"+r.person] ||= []).push(r);
 for(const r of recs){const p=(per[r.person] ||= {n:0,ok:0,q:0,qa:0,fixed:0,d:0}); p.n++; if(complete(r)) p.d++; if(r.ask&&recState(r)==="done") p.ok++; p.q+=r.quotes.length; p.qa+=r.quotes.filter(c=>(attrib[c.qid]||{}).answer).length;
  if(correctionAnswers[r.key]?.edits?.length) p.fixed++;}
 let h="", lastSec=null;
 for(const [gk,rs] of Object.entries(groups)){
  const person=rs[0].person, p=per[person], sec=section(rs[0]);
  if(HAS_DRAFTS && sec!==lastSec){lastSec=sec;
   const all=recs.filter(r=>section(r)===sec);
   h+=`<div class="sec" title="${esc(SECTION_HELP[sec])}">${SECTIONS[sec]} <small>· ${all.filter(complete).length} of ${all.length} checked</small></div>`;}
  h+= CAPTIONS_ONLY
   ? `<div class="grp"><b>${esc(person)}</b><span title="recordings with a saved caption correction">${p.fixed} of ${p.n} corrected</span></div>`
   : QUOTE_ONLY
   ? `<div class="grp"><b>${esc(person)}</b><span class="${p.qa===p.q?"ok":""}" title="quotes with a saved answer">${p.qa} of ${p.q} quotes answered</span></div>`
   : !CHECK
   ? `<div class="grp"><b>${esc(person||"Items")}</b><span class="${p.d===p.n?"ok":""}" title="items marked done or fully answered">${p.d} of ${p.n} done</span></div>`
   : `<div class="grp"><b>${esc(person)}</b><span class="${p.ok>=CHECK.goal.count?"ok":""}" title="${esc(CHECK.goal.help)}">${p.ok} verified of ${p.n}${p.q?` · ${p.q} quotes`:""}</span></div>`;
  for(const r of rs){
   const left=outstanding(r);
   h+=`<div class="item" tabindex="0" data-k="${esc(r.key)}" aria-current="${r.key===cur}"><span class="dot ${recState(r)}"></span><span class="t">${esc(r.title)}${r.quotes.length?` <span class="turn">· ${r.quotes.length} quote${r.quotes.length>1?"s":""}</span>`:""}${chips(projectsOf(r))}</span></div>`;}
 }
 el("list").innerHTML = h || `<div class="item">Nothing here. ${filter==="todo"?"Everything has an answer.":""}</div>`;
 const done=recs.filter(complete).length, q=recs.reduce((n,r)=>n+r.quotes.length,0);
 if(CAPTIONS_ONLY){const fixed=recs.filter(r=>correctionAnswers[r.key]?.edits?.length).length;
  el("count").textContent=`${fixed} of ${recs.length} recordings have caption corrections`;
  el("bar").style.width=(100*fixed/(recs.length||1)).toFixed(1)+"%"; return;}
 el("count").textContent=`${done} of ${recs.length} ${!CHECK&&!QUOTE_ONLY?"items done":"recordings checked"}${q?` · ${recs.reduce((n,r)=>n+r.quotes.filter(c=>(attrib[c.qid]||{}).answer).length,0)} of ${q} quotes`:""}`;
 el("bar").style.width=(100*done/(recs.length||1)).toFixed(1)+"%";}

function opt(store, id, field, value, label, key, cls=""){
 const on=(store[id]||{})[field]===value;
 const fab = store===labels && draftOpen(REC[id]) && REC[id].draft[field]===value ? ` <span class="mtag" title="the model's draft answer">model</span>` : "";
 return `<button type="button" class="opt ${esc(cls)}" data-id="${esc(id)}" data-f="${esc(field)}" data-v="${esc(JSON.stringify(value))}" aria-pressed="${on}" ${writable?"":"disabled"}>${esc(label)}${fab} <kbd>${esc(key)}</kbd></button>`;}

// The model's draft, shown as a suggestion once the questions are answered. Where the
// person's saved answer differs, the card says so, so a slip is caught before moving on.
const YN = v => v===true?"yes":v===false?"no":"?";
function draftCard(r){
 if(!HAS_DRAFTS || !r.ask) return "";
 const why=(r.doubt||[]).map(w=>`<span class="chip">${esc(w)}</span>`).join("");
 const d=r.draft;
 if(!d) return `<div class="mdraft"><div class="hd"><b>No usable model draft</b></div>${why?`<div class="chips">${why}</div>`:""}</div>`;
 // Hidden: the reasons too, since "the subject is absent" gives the answer away.
 if(!draftOpen(r)) return `<div class="mdraft"><div class="hd"><b>Model draft hidden</b><span>until every question below has an answer</span></div>
  <p>Answers given before the draft appears are blind and can measure the model. The draft then appears here to compare; an answer you change after that is marked as made with the draft on screen.</p></div>`;
 const l=labels[r.key]||{};
 // A field marked differs_only_if counts only once that other answer is true, as in needs_review.
 const differ=FIELDS.filter(f=>f.name in l && l[f.name]!==d[f.name] && !(f.differs_only_if && !l[f.differs_only_if])).map(f=>f.draft_label||f.label);
 return `<div class="mdraft">
  <div class="hd"><b>Model draft</b><span>${esc(d.model)} · confidence ${esc(d.confidence)}</span><span>a suggestion: you still pick each answer</span></div>
  <dl>${(CHECK.draft_fields||[]).map(([f,label])=>`<div><dt>${esc(label)}</dt><dd>${typeof d[f]==="string"?esc(d[f]):YN(d[f])}</dd></div>`).join("")}</dl>
  ${why?`<div class="chips">${why}</div>`:""}
  <p>${esc(d.reason)}</p>
  ${(d.evidence||[]).map(x=>`<blockquote>${esc(x)}</blockquote>`).join("")}
  ${differ.length?`<div class="differ">Your answer differs from the model's on: ${esc(differ.join(", "))}</div>`:""}
 </div>`;}

function renderDetail(){
 const r=curRec();
 if(!r){el("detail").innerHTML='<p class="sub">Pick a recording on the left.</p>';return;}
 const l=labels[r.key]||{}, start=r.duration?Math.floor(r.duration*0.2):0;
 const hints=(r.hints||[]).map(h=>`<span class="chip">${esc(h)}</span>`).join("")
  + (r.mentions!=null?`<span class="chip info">${esc(r.mentions)} mention${r.mentions===1?"":"s"} of the name in the transcript</span>`:"");
 const ask = r.ask ? `<section class="mode" data-mode="checklist"><h3 class="mode-hd">Checklist<small>about this recording</small></h3>
   ${FIELDS.map((f,i)=>{
    const cur=(f.options||[]).find(o=>o.value===l[f.name]);
    return `<div class="q"><label class="l">${i+1} · ${esc(f.label)}<small>${esc(f.help||"")}</small></label><div class="opts">${(f.options||[]).map(o=>opt(labels,r.key,f.name,o.value,o.label,o.key,o.cls||"")).join("")}</div></div>`
     + ((f.options||[]).some(o=>o.help) ? `<div class="q"><span></span><div class="def">${cur?esc(cur.help):esc(f.prompt||"")}</div></div>` : "");}).join("")}
   <div class="q"><label class="l" for="notes">Notes<small>optional</small></label><textarea id="notes" ${writable?"":"disabled"} placeholder="${esc(CHECK.notes_placeholder||"")}">${esc(l.notes||"")}</textarea></div></section>` : "";
 const helps=[...new Set(r.quotes.map(c=>S.project_help[projectOf(c)]))].join("; ");
 const quotes = r.quotes.length && HAS("speakers") ? `
  <section class="mode qblock" data-mode="speakers"><h3 class="mode-hd">Speakers<small>who says the quoted words</small></h3>
   <div class="q"><label class="l">${r.ask?(FIELDS.length+1)+" · ":""}Who says these lines?<small>${esc(helps)}</small></label>
    <div class="qhelp">Label the underlined quote words. You can also label the surrounding speech, but only the quote determines this review's result. The result updates and saves automatically.</div></div>
   ${r.quotes.map((c,i)=>`<div class="qitem" data-q="${i}" data-focus="${i===focusQ}">
     <div class="hd"><span>quote ${i+1} of ${r.quotes.length}${chips([projectOf(c)])}</span>${c.t!=null&&mediaUrl(r,c.t)?`<a href="${esc(mediaUrl(r,c.t))}" target="_blank" rel="noopener">play from ${fmtT(c.t)}</a>`:""}</div>
     ${c.title?`<b class="qtitle">${esc(c.title)}</b>`:""}${c.why?`<div class="qhelp">${esc(c.why)}</div>`:""}
     <div class="quote-exact"><b>${esc(c.question||"Quote being checked")}</b><p>${esc(c.text)}</p></div>
     <label class="note">Note on this passage <small>optional</small><textarea data-note="${esc(c.qid)}" ${writable?"":"disabled"}>${esc((reviewState.notes[c.qid]||{}).text||"")}</textarea></label>
     <div data-span-editor="${i}">Loading speaker-range editor…</div>
    </div>`).join("")}
  </section>` : "";
 // With no checklist and no passages the transcript is the whole task, so it is open.
 const whole = !r.ask && !r.quotes.length;
 const extra = (!r.quotes.length || !HAS("speakers")) && (HAS("speakers")||HAS("captions"))
  ? `<details class="range-extra" ${whole?"open":""}><summary>${HAS("speakers")?(whole?"Mark who speaks in this transcript":"Mark speaker ranges in this recording (optional)"):"Correct the captions of this recording"}</summary><div data-span-editor="-1">Loading transcript…</div></details>` : "";
 const itemNote = !r.ask ? `<label class="note">Note on this item <small>optional</small><textarea data-note="${esc(r.key)}" ${writable?"":"disabled"}>${esc((reviewState.notes[r.key]||{}).text||"")}</textarea></label>` : "";
 const doneBtn = !CAPTIONS_ONLY && !r.ask && !r.quotes.length
  ? `<button type="button" class="opt ${reviewState.done[r.key]?"y":""}" id="done" aria-pressed="${!!reviewState.done[r.key]}" ${writable?"":"disabled"}>${reviewState.done[r.key]?"Done ✓":"Mark this item done"}</button>` : "";
 el("detail").innerHTML=`
  <div class="who"><h2>${esc(r.person||r.title)}</h2><span class="role">${esc(r.role||"")}</span>${chips(projectsOf(r))}</div>
  ${r.person?`<p class="title">${esc(r.title)}</p>`:""}
  <div class="facts">${r.channel?`<span>${esc(r.channel)}</span>`:""}${r.upload?`<span>uploaded ${esc(fmtDate(r.upload))}</span>`:""}${typeof r.duration==="number"?`<span>${Math.round(r.duration/60)} min</span>`:""}${r.words?`<span>${esc(typeof r.words==="number"?r.words.toLocaleString():r.words)} words</span>`:""}</div>
  ${hints?`<div class="chips">${hints}</div>`:""}
  ${mediaUrl(r,start)?`<a class="yt" id="yt" href="${esc(mediaUrl(r,start))}" target="_blank" rel="noopener">Open the recording at ${fmtT(start)} ↗</a>`:""}
  ${r.description?`<details class="desc"><summary>Video description</summary><p>${esc(r.description)}</p></details>`:""}
  ${draftCard(r)}
  <div class="form">${ask}${quotes}
   ${extra}${itemNote}
   <div class="foot"><div class="nav"><button type="button" class="opt" id="prev">Previous <kbd>k</kbd></button><button type="button" class="opt" id="next">Next <kbd>j</kbd></button>${doneBtn}</div><span class="status" id="status"></span></div>
  </div>`;
 el("status").textContent = CAPTIONS_ONLY ? "Captions: correct any errors you notice; there is nothing else to answer"
  : complete(r) ? "All answered" : !r.ask && !r.quotes.length ? "Mark who speaks, then mark the item done"
  : `${outstanding(r)} answer${outstanding(r)===1?"":"s"} left on this recording`;
 el("detail").querySelectorAll("button[data-f]").forEach(b=>b.onclick=()=>setField(labels, b.dataset.id, b.dataset.f, JSON.parse(b.dataset.v)));
 el("detail").querySelectorAll(".qitem").forEach(n=>n.onclick=()=>{focusQ=+n.dataset.q;});
 mountSpanEditors(r);
 el("prev").onclick=()=>step(-1); el("next").onclick=()=>step(1);
 const db=el("done"); if(db) db.onclick=()=>markDone(r.key, !reviewState.done[r.key]);
 el("detail").querySelectorAll("textarea[data-note]").forEach(ta=>{let tm; ta.oninput=()=>{clearTimeout(tm); tm=setTimeout(()=>saveNote(ta.dataset.note, ta.value),700);};});
 const ta=el("notes"); if(ta){let tm; ta.oninput=()=>{clearTimeout(tm); tm=setTimeout(()=>setField(labels,r.key,"notes",ta.value,true),700);};}}

function select(k){cur=k; history.replaceState(null,"","#"+encodeURIComponent(k)); focusQ=firstOpenQuote(REC[k]); renderList(); renderDetail();
 const n=el("list").querySelector(`[data-k="${CSS.escape(k)}"]`); n&&n.scrollIntoView({block:"nearest"});}
function firstOpenQuote(r){const i=r.quotes.findIndex(c=>!(attrib[c.qid]||{}).answer); return i<0?0:i;}
function step(d){const vis=visible(), order=vis.length?vis:recs; let i=order.findIndex(r=>r.key===cur);
 if(i<0) i = d>0 ? -1 : order.length; const n=order[Math.min(Math.max(i+d,0),order.length-1)]; n&&select(n.key);}

const queue={};
function setField(store, id, f, v, quiet){
 if(!writable) return;
 const prev=store[id]||{};
 const next={...prev, key:id, [f]:v, updated_at:new Date().toISOString()};  // the server sets checked_by
 // A checklist answer changed while the draft is on screen is assisted, and says so.
 // The first such change keeps the blind answers beside the label, so calibration can
 // still compare them with the model. Answers given before the draft appears are
 // blind, and notes are not an answer.
 if(store===labels && draftOpen(REC[id]) && FIELDS.some(x=>x.name===f) && prev[f]!==v){
  if(!prev.draft_shown) next.answers_before_draft=Object.fromEntries(FIELDS.filter(x=>x.name in prev).map(x=>[x.name,prev[x.name]]));
  next.draft_shown=REC[id].draft.model;}
 store[id]=next;
 if(!quiet){renderList();renderDetail();}
 const st=el("status");st&&(st.textContent="Saving…",st.className="status");
 queue[id]=(queue[id]||Promise.resolve()).then(()=>saver.put("labels",id,next)).then(()=>{
  if(el("status")&&cur===id) el("status").textContent=complete(curRec())?"All answered":`${outstanding(curRec())} answers left on this recording`;
 }).catch(e=>{
  if(el("status")){el("status").textContent="Not saved: "+(e&&e.message||e)+". Reload and try again.";el("status").className="status err";}
 });
}

/*__SPAN_EDITOR__*/
/*__TEXT_CORRECTIONS__*/

const CHECK_KEYS={}; for(const f of FIELDS) for(const o of f.options||[]) if(o.key!=null) CHECK_KEYS[String(o.key).toLowerCase()]=[f.name,o.value];
const fieldKeys = f => {const ks=(f.options||[]).map(o=>String(o.key));
 return ks.length>2 && ks.every((k,i)=>k===String(i+1)) ? `<b>1</b>–<b>${ks.length}</b>` : ks.map(k=>`<b>${esc(k)}</b>`).join("/");};
document.addEventListener("keydown", e=>{
 if(["TEXTAREA","INPUT","SELECT"].includes(e.target.tagName)){ if(e.key==="Escape") e.target.blur(); return; }
 if(e.metaKey||e.ctrlKey||e.altKey) return;
 const r=curRec(); if(!r) return;
 const k=e.key.toLowerCase();
 // A speaker's key labels the words selected in the transcript, even while the
 // transcript has the focus (a drag leaves it there).
 if(SPEAKER_KEYS[k] && HAS("speakers") && spanLast && spanLast.startsWith(r.key+":")){
  markSelection(r, +spanLast.slice(r.key.length+1), SPEAKER_KEYS[k]); e.preventDefault(); return;}
 if(e.target.closest("[data-span-editor]")){ if(e.key==="Escape") e.target.blur(); return; }
 if(k==="j"||k==="arrowdown"){step(1);e.preventDefault();return;}
 if(k==="k"||k==="arrowup"){step(-1);e.preventDefault();return;}
 if(k==="v"){const a=el("yt"); a&&window.open(a.href,"_blank","noopener");return;}
 if(k==="]"){if(r.quotes.length){focusQ=(focusQ+1)%r.quotes.length; renderDetail(); e.preventDefault();}return;}
 if(k==="["){if(r.quotes.length){focusQ=(focusQ-1+r.quotes.length)%r.quotes.length; renderDetail(); e.preventDefault();}return;}
 if(!r.ask) return;
 const hit=CHECK_KEYS[k]; if(hit) setField(labels,r.key,hit[0],hit[1]);
});
el("list").addEventListener("click", e=>{const n=e.target.closest(".item[data-k]"); n&&select(n.dataset.k);});
el("list").addEventListener("keydown", e=>{if(e.key==="Enter"){const n=e.target.closest(".item[data-k]"); n&&select(n.dataset.k);}});
if(CAPTIONS_ONLY) for(const [id,f] of [["f-todo","todo"],["f-all","all"],["f-done","done"]]) el(id).setAttribute("aria-pressed", f===filter);
for(const [id,f] of [["f-todo","todo"],["f-all","all"],["f-done","done"]]) el(id).onclick=()=>{filter=f; for(const b of ["f-todo","f-all","f-done"]) el(b).setAttribute("aria-pressed", b===id); renderList();};
el("legend").innerHTML = `<span class="dot done inline"></span> verified · <span class="dot rej inline"></span> excluded or a misattributed quote · <span class="dot part inline"></span> partly answered`;
el("keyhelp").innerHTML = (QUOTE_ONLY || !CHECK
 ? "Keys: " : "Keys: " + FIELDS.map(f=>`${fieldKeys(f)} ${esc(f.draft_label||f.label)}`).join(" · ") + " · quotes: ")
 + "<b>[</b>/<b>]</b> move between quotes · <b>j</b>/<b>k</b> next recording"
 + (S.media ? " · <b>v</b> open video" : "")
 + (HAS("speakers") && SPEAKERS.length ? " · selected words: " + [...SPEAKERS, UNCLEAR].map(s=>`<b>${esc(s.key)}</b> ${esc(fillLabel(s.label, {person:"subject"}))}`).join(" · ") : "");
if(S.projects.length>1){const box=el("projects"); box.hidden=false;
 const opts=["all",...S.projects];
 box.innerHTML=opts.map(p=>`<button type="button" data-project="${esc(p)}" aria-pressed="${p===projectFilter}">${p==="all"?"All projects":esc(p)}</button>`).join("");
 box.onclick=e=>{const b=e.target.closest("button[data-project]"); if(!b) return; projectFilter=b.dataset.project;
  for(const x of box.querySelectorAll("button")) x.setAttribute("aria-pressed", x.dataset.project===projectFilter); renderList();};}

// Answers live in the dataset's answers file, through the labeling service. Without it
// the page is read-only. Every write sends back the versions api/answers gave this
// page; if the files changed outside it since (a git pull, a revert, a server
// restart), the server refuses with "stale" and the page stops saving until reloaded.
function tlStale(message){
 writable=false; renderFinish();
 el("banner").hidden=false;
 el("banner").textContent="These answers changed outside this page, or the server restarted. Reload the page to see the current answers; the last change was not saved. ("+message+")";
 renderDetail();
}
const localStore = {
 where: "this machine",
 versions: null,
 async load(){
  const res = await fetch("api/answers", {headers:{accept:"application/json"}});
  if(!res.ok) throw new Error("api/answers returned "+res.status);
  const j = await res.json();
  if(!j || typeof j.labels!=="object" || typeof j.attribution!=="object" || !j.versions
     || typeof j.versions.answers!=="string" || typeof j.versions.review!=="string")
   throw new Error("api/answers returned an unexpected shape");
  this.versions = j.versions;
  return j;
 },
 async post(path, body){
  const res = await fetch(path, {method:"POST", headers:{"content-type":"application/json"},
   body: JSON.stringify({...body, versions: this.versions})});
  let j={}; try{ j = await res.json(); }catch(e){}
  if(res.status===409 && j.stale){ tlStale(j.error||"stale"); throw new Error("not saved: reload the page"); }
  if(!res.ok) throw new Error(j.error || ("the server answered "+res.status));
  return j;
 },
 async put(coll, id, value){
  return this.post("api/answer", {kind:coll, id, value});
 }};
function saveNote(id, text){
 if(!writable) return;
 const st=el("status");
 saver.post("api/note", {id, text}).then(()=>{
  if(text.trim()) reviewState.notes[id]={text}; else delete reviewState.notes[id];
  st&&(st.textContent="Note saved", st.className="status");
 }).catch(e=>{st&&(st.textContent="Note not saved: "+e.message, st.className="status err");});}
function markDone(key, done){
 if(!writable) return;
 saver.post("api/done", {key, done}).then(()=>{
  if(done) reviewState.done[key]={}; else delete reviewState.done[key];
  renderList(); renderDetail();
 }).catch(e=>{const st=el("status"); st&&(st.textContent="Not saved: "+e.message, st.className="status err");});}
// Finish tells the agent that asked for these labels that the owner is done.
function renderFinish(){
 const b=el("finish"); if(!b) return;
 b.hidden=!writable; b.disabled=!!reviewState.finished;
 b.textContent=reviewState.finished ? "Finished ✓" : "Finish";
 b.onclick=()=>{ if(reviewState.finished) return;
  saver.post("api/done", {finish:true}).then(()=>{reviewState.finished={}; renderFinish();
   el("banner").hidden=false; el("banner").textContent="Finished. The agent that asked for these labels can read them now; you can close this page.";})
  .catch(e=>{el("banner").hidden=false; el("banner").textContent="Finish was not saved: "+e.message;}); };}
function startAt(){let key="";try{key=decodeURIComponent(location.hash.slice(1));}catch(e){}
 const todo=REC[key]||recs.find(r=>!complete(r)); if(todo){cur=todo.key; focusQ=firstOpenQuote(todo);}}

renderList(); renderDetail();
(async()=>{
 let local=null, why="";
 try{ local = await localStore.load(); }catch(e){ local=null; why=e&&e.message||String(e); }
 if(!local){ writable=false; el("banner").hidden=false;
  el("banner").textContent="Answers can't be saved right now: the server did not return them ("+why+"). Reload the page; if that fails, open a new login link (tl url).";
  renderDetail(); return; }
 saver=localStore; writable=true;
 labels=local.labels; attrib=local.attribution; spanAnswers=local.spans||{}; correctionAnswers=local.corrections||{};
 reviewState=local.review||reviewState; renderFinish();
 startAt(); renderList(); renderDetail();
})();
