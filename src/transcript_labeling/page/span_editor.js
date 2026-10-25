// Part of the page module. All offsets are raw whitespace-token indices,
// end-exclusive; no DOM text offsets or model drafts are saved as labels.
let spanAnswers = {};
const transcriptCache = {}, transcriptLoads = {}, spanSelections = {}, spanFull = {}, spanSample = {};
const spanUndo = {}, spanSaves = {}, spanErrors = {}, spanPending = {};
const spanEditing = {};
// The editor where words were last selected, as "<item key>:<editor index>", so a
// speaker's key can label them.
let spanLast = null;

function transcriptFor(recording){
 if(transcriptCache[recording.key]) return Promise.resolve(transcriptCache[recording.key]);
 return transcriptLoads[recording.key] ||= (async()=>{
  const res=await fetch("api/transcript?key="+encodeURIComponent(recording.key));
  if(!res.ok) throw new Error("Could not load the transcript. Reload this page after the local server is updated.");
  const data=await res.json(), expected=D.transcript_index[recording.key];
  if(data.text_sha256!==expected.text_sha256 || data.token_count!==expected.token_count)
   throw new Error("The transcript changed. Reload the page before labeling it.");
  return transcriptCache[recording.key]=data;
 })().catch(error=>{delete transcriptLoads[recording.key];throw error;});
}
function spanWindow(recording,index){
 const data=transcriptCache[recording.key];
 if(spanFull[recording.key+":"+index]) return [0,data.tokens.length];
 // An item with no reading excerpts (a dataset item, not a verbatim recording) shows whole.
 if(index<0 && !(recording.excerpts||[]).length) return [0,data.tokens.length];
 const passage=index<0 ? recording.excerpts[spanSample[recording.key]||0] : recording.quotes[index];
 return [passage?.start||0,Math.min(passage?.end||130,data.tokens.length)];
}
function humanRanges(recording){
 if(spanAnswers[recording.key])return spanAnswers[recording.key].ranges;
 // Preserve earlier whole-quote human answers; "both" supplies no boundary.
 let ranges=[];
 for(const quote of recording.quotes){const answer=attrib[quote.qid]?.answer;
  if([...SPEAKER_IDS,UNCLEAR.id].includes(answer)&&Number.isInteger(quote.quote_start))
   ranges=paintRanges(ranges,quote.quote_start,quote.quote_end,answer);}
 return ranges;
}
function draftLabels(data){
 if(data.draftLabels)return data.draftLabels;
 const values=Array.from({length:data.tokens.length},()=>new Set());
 for(const suggestion of data.suggestions) for(let i=suggestion.start;i<suggestion.end;i++) values[i].add(suggestion.speaker);
 return data.draftLabels=values.map(v=>v.size>1?"conflict":([...v][0]||"unknown"));
}
function statesFor(recording){
 const data=transcriptCache[recording.key], states=draftLabels(data).map(speaker=>({speaker,draft:true}));
 for(const range of humanRanges(recording)) for(let i=range.start;i<range.end;i++) states[i]={speaker:range.speaker,draft:false};
 return states;
}
function paintRanges(ranges,start,end,speaker,origin="human"){
 const kept=[];
 for(const range of ranges){
  if(range.end<=start||range.start>=end){kept.push({...range});continue;}
  if(range.start<start)kept.push({...range,end:start});
  if(range.end>end)kept.push({...range,start:end});
 }
 if(speaker)kept.push({start,end,speaker,origin});
 kept.sort((a,b)=>a.start-b.start);
 const merged=[];
 for(const range of kept){const last=merged.at(-1);
  if(last&&last.end===range.start&&last.speaker===range.speaker&&last.origin===range.origin)last.end=range.end;
  else merged.push(range);}
 return merged;
}
function rememberRanges(recording){(spanUndo[recording.key] ||= []).push(structuredClone(humanRanges(recording)));}
function saveRanges(recording,ranges){
 const data=transcriptCache[recording.key];
 const prior=spanAnswers[recording.key];
 if(prior&&(prior.text_sha256!==data.text_sha256||prior.token_count!==data.token_count)){
  spanErrors[recording.key]="Existing annotations refer to a different transcript; they were kept unchanged.";
  updateSpanStatus(recording);return;
 }
 const value={schema_version:1,key:recording.key,text_sha256:data.text_sha256,token_count:data.token_count,
  ranges,updated_at:new Date().toISOString(),  // the server sets checked_by
  // Assisted only when the editor showed model suggestions for this recording.
  assisted:data.suggestions.length>0,
  suggestions_sha256:data.suggestions_sha256};
 spanAnswers[recording.key]=value;spanErrors[recording.key]="";
 spanPending[recording.key]=(spanPending[recording.key]||0)+1;
 spanSaves[recording.key]=(spanSaves[recording.key]||Promise.resolve()).then(()=>saver.put("spans",recording.key,value)).then(result=>{
  if(result?.attribution) Object.assign(attrib,result.attribution);
  spanErrors[recording.key]="";
 }).catch(error=>{spanErrors[recording.key]=error.message||String(error);}).finally(()=>{
  spanPending[recording.key]--;updateSpanStatus(recording);renderList();
  if(cur===recording.key) el("status").textContent=complete(recording)?"All answered":`${outstanding(recording)} answers left on this recording`;
 });
 for(const quote of recording.quotes) attrib[quote.qid]={...(attrib[quote.qid]||{}),answer:null,review_status:"saving"};
 renderList();drawAllSpans(recording);
}
function updateSpanStatus(recording){
 if(cur!==recording.key)return;
 document.querySelectorAll(".span-status").forEach(node=>{
  node.classList.toggle("err",!!spanErrors[recording.key]);
  node.textContent=spanErrors[recording.key]?"Not saved: "+spanErrors[recording.key]:spanPending[recording.key]?"Saving markings…":
   spanAnswers[recording.key]?"Markings saved on this machine":"Suggestions are drafts until you confirm them";
 });
 document.querySelectorAll("[data-retry]").forEach(button=>button.hidden=!spanErrors[recording.key]);
}
function markSelection(recording,index,speaker){
 if(!writable)return;
 const selection=spanSelections[recording.key+":"+index];if(!selection)return;
 rememberRanges(recording);
 let ranges=humanRanges(recording);const id=recording.key+":"+index,prior=spanEditing[id];
 if(prior)ranges=paintRanges(ranges,prior[0],prior[1],null);
 delete spanEditing[id];
 saveRanges(recording,paintRanges(ranges,selection[0],selection[1],speaker));
}
function confirmDrafts(recording,index){
 if(!writable)return;
 const [start,end]=spanWindow(recording,index), states=statesFor(recording);let ranges=humanRanges(recording);
 rememberRanges(recording);
 for(let i=start;i<end;){const label=states[i];let j=i+1;
  while(j<end&&states[j].speaker===label.speaker&&states[j].draft===label.draft)j++;
  if(label.draft&&SPEAKER_IDS.includes(label.speaker))ranges=paintRanges(ranges,i,j,label.speaker,"model_confirmed");
  i=j;
 }
 saveRanges(recording,ranges);
}
function speechIndices(data,start,end){
 const indices=[];for(let i=start;i<end;i++) if(data.token_kinds[i]==="speech")indices.push(i);return indices;
}
function speechText(data,start,end){return speechIndices(data,start,end).map(i=>data.tokens[i]).join(" ");}
function quoteReview(recording,index){
 const quote=recording.quotes[index],data=transcriptCache[recording.key],ranges=humanRanges(recording);
 const words=speechIndices(data,quote.quote_start,quote.quote_end),speakers=new Set(),missing=[];let unsure=0;
 for(const i of words){const range=ranges.find(r=>r.start<=i&&i<r.end);
  if(!range)missing.push(i);else{speakers.add(range.speaker);if(range.speaker===UNCLEAR.id)unsure++;}}
 const answer=missing.length||!words.length?null:unsure?UNCLEAR.id:speakers.size>1?MIXED:[...speakers][0];
 return {answer,missing,unsure,total:words.length,reviewed:words.length-missing.length};
}
function reviewSummary(recording,index){
 const state=quoteReview(recording,index);
 const quote=recording.quotes[index],corrected=editsFor(recording).some(e=>e.start<quote.quote_end&&e.end>quote.quote_start);
 let title,detail;
 if(state.missing.length){title=`${state.missing.length} quote words still need review`;
  detail=`${state.reviewed} of ${state.total} quote words reviewed. Label the remaining underlined words. Surrounding speech does not change this result.`;}
 else if(!state.total){title="No readable quote words";detail="This quote contains only caption markers; it remains unfinished.";}
 else {title=state.answer===MIXED?"Quote includes more than one speaker":state.answer===UNCLEAR.id?"Quote reviewed — speaker uncertain":`Quote spoken by ${speakerLabel(state.answer,recording)}`;
  detail=state.unsure?`You marked ${state.unsure} quote words as unsure. This is saved as an uncertain review.`:"Calculated from your reviewed quote words and saved automatically. No second answer is needed.";}
 return `<div class="quote-summary ${state.answer?"complete":"pending"}" role="status"><strong>${esc(title)}</strong><p>${esc(detail)}</p>${corrected?'<p>You corrected words in this quote. The quote above and review counts refer to the original captions the judge used; the corrected reading appears in the passage.</p>':""}${state.missing.length?'<button type="button" class="opt" data-next-unreviewed>Review remaining quote words</button>':""}</div>`;
}
function spanToolbar(recording,index){
 const node=document.querySelector(`[data-span-editor="${index}"]`);if(!node)return;
 const id=recording.key+":"+index,selection=spanSelections[id],data=transcriptCache[recording.key];
 const text=selection ? readingText(recording,...selection) : "Drag across words below to label the speaker or correct the text.";
 const hasEdit=selection&&editsFor(recording).some(e=>e.start<selection[1]&&e.end>selection[0]);
 node.querySelector(".span-selection").textContent=selection?`${hasEdit?"Selected text":`Selected ${speechIndices(data,...selection).length} words`}: ${text}`:text;
 const correct=node.querySelector('[data-correct-text]');if(correct)correct.disabled=!selection||!writable||!!correctionPending[recording.key];
 node.querySelectorAll("[data-paint]").forEach(b=>b.disabled=!selection||!speechIndices(data,...selection).length||!writable);
 node.querySelectorAll("[data-w]").forEach(word=>word.classList.toggle("selected-word",!!selection&&+word.dataset.end>selection[0]&&+word.dataset.w<selection[1]));
 const sliders=node.querySelector(".span-boundaries");sliders.hidden=!selection;
 if(selection){
  const [windowLo,windowHi]=spanWindow(recording,index), speech=speechIndices(data,windowLo,windowHi);
  const lo=speech[0]??windowLo,hi=(speech.at(-1)??(windowHi-1))+1;
  for(const [field,value,min,max] of [["start",selection[0],lo,selection[1]-1],["end",selection[1],selection[0]+1,hi]]){
   const input=sliders.querySelector(`[data-edge="${field}"]`);input.min=min;input.max=max;input.value=value;
   input.setAttribute("aria-valuetext",data.tokens[field==="start"?value:value-1]);
  }
 }
}
function drawAllSpans(recording){
 if(cur!==recording.key)return;
 document.querySelectorAll("[data-span-editor]").forEach(node=>drawSpanEditor(recording,+node.dataset.spanEditor));
}
function drawSpanEditor(recording,index){
 const root=document.querySelector(`[data-span-editor="${index}"]`);if(!root||cur!==recording.key)return;
 const scrollTop=root.querySelector(".span-transcript")?.scrollTop||0;
 const data=transcriptCache[recording.key];if(!data)return;
 const saved=spanAnswers[recording.key];
 const corrected=correctionAnswers[recording.key];
 if([saved,corrected].some(s=>s&&(s.text_sha256!==data.text_sha256||s.token_count!==data.token_count))){
  root.textContent="Saved markings refer to a different transcript. They have been preserved; resolve the changed transcript before editing.";return;
 }
 const id=recording.key+":"+index,[lo,hi]=spanWindow(recording,index),states=statesFor(recording),edits=editsFor(recording);
 const quote=index>=0?recording.quotes[index]:null;
 const spoken=speechIndices(data,lo,hi);
 const markable=spoken.some(i=>states[i].draft&&SPEAKER_IDS.includes(states[i].speaker));
 const segments=data.segments||[], segHead=new Map(segments.map(s=>[s.header[0],s]));
 const known=spoken.filter(i=>draftLabels(data)[i]!=="unknown").length;
 const timestamps=new Map(data.times);
 let words="";
 for(let i=lo;i<hi;i++){
  const edit=edits.find(e=>e.start<=i&&i<e.end),start=edit?.start??i,end=edit?.end??(i+1);
  let state=states[i];
  if(edit&&states.slice(start,end).some(s=>s.speaker!==state.speaker||s.draft!==state.draft))state={speaker:"conflict",draft:true};
  const token=edit?.replacement??data.tokens[i],isQuote=quote&&start<quote.quote_end&&end>quote.quote_start;
  const tip=state.speaker==="unknown"?"Unknown — no attribution":state.speaker==="conflict"?(edit?"This corrected phrase has mixed markings. Label it as a whole, or restore the original to label words separately.":"Models disagree — review this text"):
   `${state.draft?"Model suggestion (unconfirmed)":"Human reviewed"}: ${speakerLabel(state.speaker,recording)}`;
  const kind=data.token_kinds[i];
  if(kind==="meta"){
   // A caption segment's header (its clock time and original label) is shown, never
   // labelled; clicking it selects the caption's words. Other header text (notes
   // before the first caption) is shown muted.
   const seg=segHead.get(i);
   if(seg) words+=`<span class="seg-head" contenteditable="false" data-seg="${seg.body[0]},${seg.body[1]}" title="Select this caption">${fmtT(seg.t_start)} · ${esc(seg.label||"no label")}</span>`;
   else if(!segments.some(s=>s.header[0]<=i&&i<s.header[1])) words+=`<span class="caption-meta" contenteditable="false">${esc(data.tokens[i])} </span>`;
  }else if(kind==="timestamp"){
   const link=mediaUrl(recording,timestamps.get(i));
   if(spanFull[id]) words+=`<span class="caption-time" contenteditable="false">${link?`<a href="${esc(link)}" target="_blank" rel="noopener">▶ ${fmtT(timestamps.get(i))}</a>`:fmtT(timestamps.get(i))}</span>`;
  }else if(kind==="turn") words+='<span class="caption-turn" contenteditable="false" aria-label="Caption speaker break"> / </span>';
  else if(kind!=="speech"){
   if(kind==="gap"&&(i===lo||data.token_kinds[i-1]!=="gap")) words+='<span class="caption-gap" contenteditable="false" title="Words missing or obscured in the captions; not part of speaker labeling">caption gap</span> ';
  }else words+=`<span data-w="${start}" data-end="${end}" class="word ${speakerClass(state.speaker)} ${state.draft?"draft":"reviewed"} ${isQuote?"evidence":""} ${edit?"corrected":""}" title="${esc(tip+(edit?'. Text corrected; original: '+edit.original:''))}">${esc(token)} </span>`;
  if(edit)i=end-1;

 }
 const SPK=HAS("speakers"),CAP=HAS("captions");
 const marked=humanRanges(recording).filter(range=>range.start<hi&&range.end>lo&&speechIndices(data,Math.max(lo,range.start),Math.min(hi,range.end)).length);
 root.innerHTML=`<div class="span-top"><b>${SPK?"Mark who is speaking":"Correct the captions"}</b>${index<0&&!(recording.excerpts||[]).length?"":`<button type="button" class="opt" data-expand>${spanFull[id]?"Back to passage":"Expand to full transcript"}</button>`}</div>
  <p class="span-instructions">Select words, then ${SPK&&CAP?"choose who said them or use Correct text for caption errors":SPK?"choose who said them":"use Correct text for caption errors"}. Caption gaps and playback times are skipped automatically.</p>
  <div class="span-legend">${SPEAKERS.map(sp=>`<span class="${speakerClass(sp.id)} reviewed">${esc(speakerLabel(sp.id,recording))} · reviewed</span>`).join("")}<span class="unclear reviewed">${esc(UNCLEAR.label)}</span><span class="${speakerClass(SPEAKER_IDS[0])} draft">Dashed · model suggestion</span><span>Plain · not reviewed</span>${quote?'<span class="evidence">Underline · passage being checked</span>':""}</div>
  ${data.suggestions_hidden?`<p class="span-model"><button type="button" class="opt" data-reveal ${writable?"":"disabled"}>Show model suggestions (${data.suggestions_hidden})</button> Hidden until you ask, so what you mark first is your own reading. The ranges you have marked so far are kept as your blind answer.</p>`:""}
  <p class="span-model">${known?"Model estimates cover "+known+" of these "+spoken.length+" words. Confirm or correct the dashed suggestions.":"No model suggestions for this passage. Label the words you can identify; use “Unsure” when you cannot identify the speaker."} ${states.slice(lo,hi).some(s=>s.speaker==="conflict")?"Models disagree on the dotted words; review those yourself.":""}</p>
  ${index<0&&!spanFull[id]?`<label class="span-samples">Passage <select data-sample>${(recording.excerpts||[]).map((e,i)=>`<option value="${i}" ${i===(spanSample[recording.key]||0)?"selected":""}>${Math.round(e.at*100)}% into recording</option>`).join("")}</select></label>`:""}
  <div class="span-actions"><div class="span-selection" aria-live="polite"></div><div class="opts">
   ${SPK?`${SPEAKERS.map(sp=>`<button class="opt ${speakerClass(sp.id)}" type="button" data-paint="${esc(sp.id)}">${esc(speakerLabel(sp.id,recording))} <kbd>${esc(sp.key)}</kbd></button>`).join("")}
   <button class="opt" type="button" data-paint="${esc(UNCLEAR.id)}">${esc(UNCLEAR.label)} <kbd>${esc(UNCLEAR.key)}</kbd></button>
   <button class="opt" type="button" data-paint="">Clear human marks</button>`:""}${CAP?'<button class="opt" type="button" data-correct-text>Correct text</button>':""}</div>
   <div class="span-boundaries" hidden><label>Selection start <input type="range" data-edge="start" aria-label="Selection start word"></label><label>Selection end <input type="range" data-edge="end" aria-label="Selection end word"></label></div>
  </div>
  ${correctionPanel(recording,index,lo,hi)}
  <div class="span-transcript" tabindex="0" role="region" aria-label="Transcript passage for speaker annotation">${words}</div>
  <div class="opts">${SPK?`<button type="button" class="opt" data-confirm ${!writable||!markable?"disabled":""}>Confirm suggested spans in ${spanFull[id]?"full transcript":"this passage"}</button>`:""}
   <button type="button" class="opt" data-undo ${!writable||!spanUndo[recording.key]?.length?"disabled":""}>Undo marking</button>
   <button type="button" class="opt" data-retry ${!spanErrors[recording.key]?"hidden":""}>Retry save</button></div>
  <div class="span-status status" role="status"></div>
  <details class="span-ranges"><summary>${marked.length} reviewed span${marked.length===1?"":"s"} in view</summary>
   ${marked.map(range=>`<div class="span-range"><b>${esc(speakerLabel(range.speaker,recording))}</b> <span>${esc(speechText(data,range.start,Math.min(range.end,range.start+12)))}${range.end-range.start>12?"…":""}</span><button type="button" class="opt" data-select-span="${range.start},${range.end}">Select / adjust</button><button type="button" class="opt" data-remove-span="${range.start},${range.end}" ${writable?"":"disabled"}>Remove</button></div>`).join("")}
  </details>
  ${quote?reviewSummary(recording,index):""}`;

 const expand=root.querySelector("[data-expand]");
 if(expand)expand.onclick=()=>{spanFull[id]=!spanFull[id];delete spanSelections[id];delete spanEditing[id];drawSpanEditor(recording,index);};
 root.querySelectorAll("[data-seg]").forEach(head=>head.onclick=()=>{
  const [a,b]=head.dataset.seg.split(",").map(Number);if(b<=a)return;
  spanSelections[id]=correctionBounds(recording,a,b);delete spanEditing[id];spanLast=id;spanToolbar(recording,index);
 });
 const reveal=root.querySelector("[data-reveal]");
 if(reveal)reveal.onclick=()=>{
  if(!writable)return;
  reveal.disabled=true;
  // Reveal after every span save already sent, so ranges_before_reveal holds the last blind mark.
  spanSaves[recording.key]=(spanSaves[recording.key]||Promise.resolve()).catch(()=>{}).then(()=>{if(!writable)throw new Error("this page can no longer save; reload it");return saver.post("api/reveal",{key:recording.key});}).then(res=>{
   data.suggestions=res.suggestions;data.suggestions_hidden=0;delete data.draftLabels;drawAllSpans(recording);
  }).catch(error=>{reveal.disabled=false;spanErrors[recording.key]=error.message||String(error);updateSpanStatus(recording);});
 };
 const sample=root.querySelector("[data-sample]");if(sample)sample.onchange=()=>{spanSample[recording.key]=+sample.value;delete spanSelections[id];drawSpanEditor(recording,index);};
 const area=root.querySelector(".span-transcript");
 area.scrollTop=scrollTop;
 const capture=()=>{
  const selection=window.getSelection();if(!selection||selection.isCollapsed||!selection.rangeCount)return;
  const range=selection.getRangeAt(0);
  if(!area.contains(range.startContainer)||!area.contains(range.endContainer))return;
  const selected=[...area.querySelectorAll("[data-w]")].filter(word=>range.intersectsNode(word));
  if(selected.length){delete spanEditing[id];spanSelections[id]=[+selected[0].dataset.w,+selected.at(-1).dataset.end];spanLast=id;spanToolbar(recording,index);}
 };
 area.onmouseup=capture;area.onkeyup=capture;
 root.querySelectorAll("[data-paint]").forEach(button=>button.onclick=()=>markSelection(recording,index,button.dataset.paint||null));
 root.querySelectorAll("[data-edge]").forEach(input=>input.oninput=()=>{
  const selection=spanSelections[id];let edge=+input.value;
  if(input.dataset.edge==="start"){while(edge<selection[1]-1&&data.token_kinds[edge]!=="speech")edge++;selection[0]=edge;}
  else{while(edge>selection[0]+1&&data.token_kinds[edge-1]!=="speech")edge--;selection[1]=edge;}
  spanSelections[id]=correctionBounds(recording,...selection);
  spanToolbar(recording,index);
 });
 const confirm=root.querySelector("[data-confirm]");if(confirm)confirm.onclick=()=>confirmDrafts(recording,index);
 root.querySelector("[data-undo]").onclick=()=>{const ranges=spanUndo[recording.key]?.pop();if(ranges)saveRanges(recording,ranges);};
 root.querySelector("[data-retry]").onclick=()=>saveRanges(recording,humanRanges(recording));
 root.querySelectorAll("[data-select-span]").forEach(button=>button.onclick=()=>{
  const original=button.dataset.selectSpan.split(",").map(Number),range=correctionBounds(recording,...original);spanEditing[id]=[...original];
  if(range[0]<lo||range[1]>hi){spanFull[id]=true;spanSelections[id]=range;drawSpanEditor(recording,index);}
  else{spanSelections[id]=range;spanToolbar(recording,index);}
 });
 root.querySelectorAll("[data-remove-span]").forEach(button=>button.onclick=()=>{
  const [a,b]=button.dataset.removeSpan.split(",").map(Number);rememberRanges(recording);saveRanges(recording,paintRanges(humanRanges(recording),a,b,null));
 });
 const remaining=root.querySelector("[data-next-unreviewed]");if(remaining)remaining.onclick=()=>{
  const missing=quoteReview(recording,index).missing;if(!missing.length)return;
  const start=missing[0];let end=start+1;
  while(end<quote.quote_end&&(data.token_kinds[end]!=="speech"||missing.includes(end)))end++;
  spanSelections[id]=correctionBounds(recording,start,end);delete spanEditing[id];spanToolbar(recording,index);
  root.querySelector(`[data-w="${spanSelections[id][0]}"]`)?.scrollIntoView({block:"center",behavior:"smooth"});
 };
 bindCorrections(root,recording,index);
 spanToolbar(recording,index);updateSpanStatus(recording);
}
function mountSpanEditors(recording){
 if(!D.transcript_index?.[recording.key])return;
 transcriptFor(recording).then(()=>drawAllSpans(recording)).catch(error=>{
  if(cur===recording.key)document.querySelectorAll("[data-span-editor]").forEach(node=>node.textContent=error.message);
 });
}
window.addEventListener("beforeunload",event=>{
 if(Object.values(spanPending).some(Boolean)||Object.values(spanErrors).some(Boolean)){
  event.preventDefault();event.returnValue="";
 }
});
