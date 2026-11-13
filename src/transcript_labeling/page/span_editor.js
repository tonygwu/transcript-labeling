// Part of the page module. All offsets are raw whitespace-token indices,
// end-exclusive; no DOM text offsets or model drafts are saved as labels.
let spanAnswers = {};
const transcriptCache = {}, transcriptLoads = {}, spanSelections = {}, spanFull = {}, spanSample = {};
const spanUndo = {}, spanSaves = {}, spanErrors = {}, spanPending = {};
const spanEditing = {}, spanSourceView = {};
// Display state only. Stored passages, target words and bulk-confirm bounds never change.
const spanContexts = {};
const CONTEXT_SECONDS = 45, CONTEXT_WORDS = 120;
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
function spanReviewWindow(recording,index){
 const data=transcriptCache[recording.key];
 // An item with no reading excerpts (a dataset item, not a verbatim recording) shows whole.
 if(index<0 && !(recording.excerpts||[]).length) return [0,data.tokens.length];
 const passage=index<0 ? recording.excerpts[spanSample[recording.key]||0] : recording.quotes[index];
 return [passage?.start||0,Math.min(passage?.end||130,data.tokens.length)];
}
function contextKey(recording,index){
 return recording.key+":"+index+(index<0?":"+(spanSample[recording.key]||0):"");
}
function contextCaptions(data){
 if(data.segments?.length)return data.segments.map(s=>({
  start:s.header[0],end:s.body[1],time:s.t_start,endTime:s.t_end??s.t_start
 }));
 // Without source segments, a timestamp and the words up to the next mark form
 // one indivisible context interval. Do not invent a time for unmarked text.
 return (data.times||[]).map(([start,time],i,marks)=>({
  start,end:marks[i+1]?.[0]??data.tokens.length,time,endTime:marks[i+1]?.[1]??time
 }));
}
function completeContextCaptions(data,[lo,hi]){
 for(const caption of contextCaptions(data))if(caption.start<hi&&caption.end>lo){
  lo=Math.min(lo,caption.start);hi=Math.max(hi,caption.end);
 }
 return [lo,hi];
}
function widenContext(data,bounds,side){
 let [lo,hi]=completeContextCaptions(data,bounds);
 const captions=contextCaptions(data),before=side==="before";
 const edge=captions.find(c=>before?c.start<=lo&&lo<c.end:c.start<hi&&hi<=c.end);
 const time=before?edge?.time:edge?.endTime;
 const candidates=captions.filter(c=>before?c.start<lo:c.end>hi);
 if(Number.isFinite(time)&&candidates.length&&candidates.every(c=>Number.isFinite(before?c.endTime:c.time))){
  const nearby=candidates.filter(c=>before?c.endTime>=time-CONTEXT_SECONDS:c.time<=time+CONTEXT_SECONDS);
  // A pause longer than the step still reveals the next caption on a button press.
  const added=nearby.length?nearby:[before?candidates.at(-1):candidates[0]];
  if(before)lo=Math.min(lo,...added.map(c=>c.start));
  else hi=Math.max(hi,...added.map(c=>c.end));
 }else{
  let words=0;
  if(before)while(lo>0&&words<CONTEXT_WORDS){lo--;if(data.token_kinds[lo]==="speech")words++;}
  else while(hi<data.tokens.length&&words<CONTEXT_WORDS){if(data.token_kinds[hi]==="speech")words++;hi++;}
 }
 return completeContextCaptions(data,[lo,hi]);
}
function spanWindow(recording,index){
 const data=transcriptCache[recording.key];
 if(spanFull[recording.key+":"+index])return [0,data.tokens.length];
 const key=contextKey(recording,index);
 if(!spanContexts[key]){
  const original=spanReviewWindow(recording,index);
  const before=widenContext(data,original,"before"),after=widenContext(data,original,"after");
  spanContexts[key]=[before[0],after[1]];
 }
 return spanContexts[key];
}
function expandSpanContext(recording,index,side){
 if(!["before","after"].includes(side))throw new Error("Unknown context direction: "+side);
 spanContexts[contextKey(recording,index)]=widenContext(transcriptCache[recording.key],spanWindow(recording,index),side);
}
function humanRanges(recording){
 if(spanAnswers[recording.key]){
  const saved=spanAnswers[recording.key];
  return saved.ranges.map(range=>{
   if(!["model_confirmed","suggestion_confirmed"].includes(range.origin))return range;
   if(range.suggestion_evidence_scope!==undefined||range.suggestions_sha256!==undefined||range.suggestion_bundle_sha256!==undefined)return range;
   const evidence={};
   for(const field of ["suggestions_sha256","suggestion_bundle_sha256"])
    if(range[field]===undefined&&saved[field]!==undefined)evidence[field]=saved[field];
   evidence.suggestion_evidence_scope="legacy_annotation";
   return {...evidence,...range};
  });
 }
 // Preserve earlier whole-quote human answers; "both" supplies no boundary.
 let ranges=[];
 for(const quote of recording.quotes){const answer=attrib[quote.qid]?.answer;
  if([...SPEAKER_IDS,UNCLEAR.id].includes(answer)&&Number.isInteger(quote.quote_start))
   ranges=paintRanges(ranges,quote.quote_start,quote.quote_end,answer);}
 return ranges;
}
function draftStates(data){
 if(data.draftStates)return data.draftStates;
 const values=Array.from({length:data.tokens.length},()=>({speakers:new Set(),sources:new Set()}));
 for(const suggestion of data.suggestions) for(let i=suggestion.start;i<suggestion.end;i++){
  values[i].speakers.add(suggestion.speaker);values[i].sources.add(suggestion.source||"unspecified");
 }
 return data.draftStates=values.map(v=>({speaker:v.speakers.size>1?"conflict":([...v.speakers][0]||"unknown"),
  draft:true,source:v.sources.size>1?"multiple":([...v.sources][0]||"none")}));
}
function draftLabels(data){return draftStates(data).map(s=>s.speaker);}
function applyRevealedSuggestions(data,res){
 data.suggestions=res.suggestions;data.suggestions_hidden=0;
 data.suggestions_sha256=res.suggestions_sha256;
 data.suggestion_bundle_sha256=res.suggestion_bundle_sha256;
 if(res.suggestion_provenance)data.suggestion_provenance=res.suggestion_provenance;
 else delete data.suggestion_provenance;
 delete data.draftStates;
}
function statesFor(recording){
 const data=transcriptCache[recording.key], states=draftStates(data).map(s=>({...s}));
 for(const range of humanRanges(recording)) for(let i=range.start;i<range.end;i++) states[i]={speaker:range.speaker,draft:false,source:range.origin||"human"};
 return states;
}
function attributionDescription(state){
 // Historical model_confirmed also covered imported tags; its name alone cannot
 // establish that a model ran. The saved suggestion hash retains the evidence.
 if(!state.draft)return state.source==="model_confirmed"?"Human confirmed suggestion":state.source==="suggestion_confirmed"?"Human confirmed imported suggestion":"Human reviewed";
 if(state.speaker==="unknown")return "No attribution";
 if(state.speaker==="conflict")return "Conflicting suggestions";
 if(state.source==="segment_label")return "Imported transcriber tag (unconfirmed)";
 if(state.source==="contextual_model")return state.speaker===UNCLEAR.id?"Contextual model uncertainty (unconfirmed)":"Contextual model suggestion (unconfirmed)";
 return state.source==="multiple"?"Suggestions from multiple sources (unconfirmed)":"Imported suggestion (unconfirmed)";
}
function sameAttribution(a,b){return a.speaker===b.speaker&&a.draft===b.draft&&a.source===b.source;}
function displayUnit(data,states,edits,i){
 const edit=edits.find(e=>e.start<=i&&i<e.end),start=edit?.start??i,end=edit?.end??(i+1);
 let state=states[i];
 if(edit&&states.slice(start,end).some(s=>!sameAttribution(s,state)))state={speaker:"conflict",draft:true,source:"multiple"};
 return {kind:data.token_kinds[i],start,end,text:edit?.replacement??data.tokens[i],state,edit};
}
function transcriptTurns(data,states,edits,lo,hi){
 // Group only the reading. Every word and correction retains its raw-token anchor.
 // No length or time-gap heuristic changes a turn. Attribution changes, explicit
 // caption turn marks, gaps and notes break it. Unknown speech follows source labels.
 const segments=data.segments||[], owners=new Map(),headers=new Set();
 for(const seg of segments){
  for(let i=seg.header[0];i<seg.header[1];i++)headers.add(i);
  for(let i=seg.body[0];i<seg.body[1];i++)owners.set(i,seg);
 }
 const parts=[];let turn=null;
 for(let i=lo;i<hi;i++){
  if(headers.has(i))continue;
  const unit=displayUnit(data,states,edits,i),seg=owners.get(i);
  if(unit.kind!=="speech"){
   if(turn&&(unit.kind==="timestamp"||unit.kind==="punctuation"))turn.units.push(unit);
   else {parts.push(unit);turn=null;}
   continue;
  }
  const label=seg?.label||"";
  if(!turn||!sameAttribution(turn.state,unit.state)||(unit.state.speaker==="unknown"&&turn.sourceLabel!==label)){
   turn={kind:"turn",start:unit.start,end:unit.end,state:unit.state,sourceLabel:label,units:[],segments:[]};parts.push(turn);
  }
  turn.units.push(unit);turn.end=unit.end;
  if(seg&&!turn.segments.includes(seg))turn.segments.push(seg);
  if(unit.edit)i=unit.end-1;
 }
 return parts;
}
function wordMarkup(unit,recording,quote){
 const {start,end,state,edit}=unit,isQuote=quote&&start<quote.quote_end&&end>quote.quote_start;
 const tip=state.speaker==="conflict"&&edit?"This corrected phrase has mixed markings. Label it as a whole, or restore the original to label words separately.":
  attributionDescription(state)+(state.speaker==="unknown"||state.speaker==="conflict"?"":": "+speakerLabel(state.speaker,recording));
 return `<span data-w="${start}" data-end="${end}" class="word ${speakerClass(state.speaker)} ${state.draft?"draft":"reviewed"} ${isQuote?"evidence":""} ${edit?"corrected":""}" title="${esc(tip+(edit?'. Text corrected; original: '+edit.original:''))}">${esc(unit.text)} </span>`;
}
function markerMarkup(unit){
 const cls=unit.kind==="meta"?"caption-meta":unit.kind==="timestamp"?"caption-marker-time":unit.kind==="gap"?"caption-gap":unit.kind==="turn"?"caption-turn":"caption-punctuation";
 return `<span class="${cls}" contenteditable="false">${esc(unit.text)} </span>`;
}
function sourceHeader(data,seg){
 // Show the original interval, including fractional seconds, and the original label.
 return esc(data.tokens.slice(seg.header[0],seg.header[1]).join(" "));
}
function conversationMarkup(data,states,edits,lo,hi,recording,quote){
 return transcriptTurns(data,states,edits,lo,hi).map(part=>{
  if(part.kind!=="turn"||!part.units)return markerMarkup(part);
  const label=part.state.speaker==="unknown"?"Unassigned":part.state.speaker==="conflict"?"Mixed attribution":speakerLabel(part.state.speaker,recording);
  const accept=HAS("speakers")&&part.state.draft&&SPEAKER_IDS.includes(part.state.speaker);
  return `<section class="conversation-turn"><div class="turn-heading"><button type="button" class="turn-speaker" data-seg="${part.start},${part.end}" title="Select this turn">${esc(label)}</button><span>${esc(attributionDescription(part.state))}</span>${accept?`<button type="button" class="opt turn-accept" data-accept-turn="${part.start},${part.end}" data-speaker="${esc(part.state.speaker)}" data-source="${esc(part.state.source)}" aria-label="Accept ${esc(label)} suggestion" ${writable?"":"disabled"}>Accept</button>`:""}</div>
   <div class="turn-words">${part.units.map(u=>u.kind==="speech"?wordMarkup(u,recording,quote):markerMarkup(u)).join("")}</div>
   ${part.segments.length?`<details class="turn-source"><summary>${part.segments.length} original caption${part.segments.length===1?"":"s"}</summary><p>Original caption times and labels. A caption may contain more than one speaker.</p>${part.segments.map(seg=>`<button type="button" class="seg-head" data-seg="${seg.body[0]},${seg.body[1]}" title="Select this original caption">${sourceHeader(data,seg)}</button>`).join("")}</details>`:""}</section>`;
 }).join("");
}
function paintRanges(ranges,start,end,speaker,origin="human",evidence={}){
 const kept=[];
 for(const range of ranges){
  if(range.end<=start||range.start>=end){kept.push({...range});continue;}
  if(range.start<start)kept.push({...range,end:start});
  if(range.end>end)kept.push({...range,start:end});
 }
 if(speaker)kept.push({start,end,speaker,origin,...evidence});
 kept.sort((a,b)=>a.start-b.start);
 const merged=[];
 for(const range of kept){const last=merged.at(-1);
  if(last&&last.end===range.start&&last.speaker===range.speaker&&last.origin===range.origin&&
    last.suggestions_sha256===range.suggestions_sha256&&last.suggestion_bundle_sha256===range.suggestion_bundle_sha256&&
    last.suggestion_evidence_scope===range.suggestion_evidence_scope)last.end=range.end;
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
  // Assisted only when the editor showed imported or contextual suggestions.
  assisted:data.suggestions.length>0,
  suggestions_sha256:data.suggestions_sha256,
  ...(data.suggestion_bundle_sha256?{suggestion_bundle_sha256:data.suggestion_bundle_sha256}:{})};
 spanAnswers[recording.key]=value;spanErrors[recording.key]="";
 spanPending[recording.key]=(spanPending[recording.key]||0)+1;
 spanSaves[recording.key]=(spanSaves[recording.key]||Promise.resolve()).then(()=>saver.put("spans",recording.key,value)).then(result=>{
  if(result?.attribution) Object.assign(attrib,result.attribution);
  spanErrors[recording.key]="";
 }).catch(error=>{spanErrors[recording.key]=error.message||String(error);}).finally(()=>{
  spanPending[recording.key]--;updateSpanStatus(recording);renderList();
  if(cur===recording.key) el("status").textContent=complete(recording)?"All answered":`${outstanding(recording)} answers left on this item`;
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
function confirmedRange(ranges,data,start,end,label){
 const evidence={};
 for(const field of ["suggestions_sha256","suggestion_bundle_sha256"])
  if(data[field]!==undefined)evidence[field]=data[field];
 if(Object.keys(evidence).length)evidence.suggestion_evidence_scope="range";
 return paintRanges(ranges,start,end,label.speaker,label.source==="contextual_model"?"model_confirmed":"suggestion_confirmed",evidence);
}
function acceptTurn(recording,index,start,end,speaker,source){
 if(!writable||!HAS("speakers"))return;
 const data=transcriptCache[recording.key],[lo,hi]=spanWindow(recording,index);
 // Resolve the displayed block again so an older button cannot overwrite a new human mark.
 const turn=transcriptTurns(data,statesFor(recording),editsFor(recording),lo,hi).find(part=>
  part.units&&part.start===start&&part.end===end&&part.state.speaker===speaker&&part.state.source===source);
 if(!turn||!turn.state.draft||!SPEAKER_IDS.includes(speaker))return;
 // Caption headers can lie between grouped words. Confirm only the displayed speech,
 // retaining the original anchors when a corrected phrase has a different word count.
 const words=turn.units.flatMap(unit=>unit.kind==="speech"?speechIndices(data,unit.start,unit.end):[]);
 let ranges=humanRanges(recording);
 rememberRanges(recording);
 for(let i=0;i<words.length;){
  let j=i+1;while(j<words.length&&words[j]===words[j-1]+1)j++;
  ranges=confirmedRange(ranges,data,words[i],words[j-1]+1,turn.state);i=j;
 }
 saveRanges(recording,ranges);
}
function confirmDrafts(recording,index){
 if(!writable)return;
 const [start,end]=spanReviewWindow(recording,index), states=statesFor(recording);let ranges=humanRanges(recording);
 rememberRanges(recording);
 for(let i=start;i<end;){const label=states[i];let j=i+1;
  while(j<end&&sameAttribution(states[j],label))j++;
  if(label.draft&&SPEAKER_IDS.includes(label.speaker)){
   ranges=confirmedRange(ranges,transcriptCache[recording.key],i,j,label);
  }
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
function transcriptPosition(area){
 if(!area)return null;
 const box=area.getBoundingClientRect();
 const word=[...area.querySelectorAll("[data-w]")].find(node=>{
  const rect=node.getBoundingClientRect();return rect.bottom>box.top&&rect.top<box.bottom;
 });
 return {scrollTop:area.scrollTop,word:word?.dataset.w,
  offset:word?word.getBoundingClientRect().top-box.top:0,top:box.top,
  visible:box.bottom>0&&box.top<window.innerHeight};
}
function restoreTranscriptPosition(area,position){
 if(!position)return;
 area.scrollTop=position.scrollTop;
 const word=position.word===undefined?null:area.querySelector(`[data-w="${position.word}"]`);
 if(word)area.scrollTop+=word.getBoundingClientRect().top-area.getBoundingClientRect().top-position.offset;
 // Keep both the inner reading position and the page viewport when controls wrap.
 if(position.visible)window.scrollBy(0,area.getBoundingClientRect().top-position.top);
}
function drawSpanEditor(recording,index){
 const root=document.querySelector(`[data-span-editor="${index}"]`);if(!root||cur!==recording.key)return;
 const position=transcriptPosition(root.querySelector(".span-transcript"));
 const data=transcriptCache[recording.key];if(!data)return;
 const saved=spanAnswers[recording.key];
 const corrected=correctionAnswers[recording.key];
 if([saved,corrected].some(s=>s&&(s.text_sha256!==data.text_sha256||s.token_count!==data.token_count))){
  root.textContent="Saved markings refer to a different transcript. They have been preserved; resolve the changed transcript before editing.";return;
 }
 const id=recording.key+":"+index,[lo,hi]=spanWindow(recording,index),states=statesFor(recording),edits=editsFor(recording);
 const quote=index>=0?recording.quotes[index]:null;
 const excerpts=recording.excerpts||[];
 const hasPassage=index>=0||excerpts.length>0;
 const [reviewLo,reviewHi]=spanReviewWindow(recording,index);
 const focus=quote||(hasPassage?{quote_start:reviewLo,quote_end:reviewHi}:null);
 const spoken=speechIndices(data,lo,hi);
 const markable=speechIndices(data,reviewLo,reviewHi).some(i=>states[i].draft&&SPEAKER_IDS.includes(states[i].speaker));
 const segments=data.segments||[], segHead=new Map(segments.map(s=>[s.header[0],s]));
 const drafts=draftLabels(data),known=spoken.filter(i=>drafts[i]!=="unknown").length;
 const timestamps=new Map(data.times);
 let words=spanSourceView[id]?"":conversationMarkup(data,states,edits,lo,hi,recording,focus);
 for(let i=lo;spanSourceView[id]&&i<hi;i++){
  const unit=displayUnit(data,states,edits,i);
  const kind=data.token_kinds[i];
  if(kind==="meta"){
   // A caption segment's header (its clock time and original label) is shown, never
   // labelled; clicking it selects the caption's words. Other header text (notes
   // before the first caption) is shown muted.
   const seg=segHead.get(i);
   if(seg) words+=`<button type="button" class="seg-head" data-seg="${seg.body[0]},${seg.body[1]}" title="Select this original caption">Original: ${sourceHeader(data,seg)}</button>`;
   else if(!segments.some(s=>s.header[0]<=i&&i<s.header[1])) words+=`<span class="caption-meta" contenteditable="false">${esc(data.tokens[i])} </span>`;
  }else if(kind==="timestamp"){
   const link=mediaUrl(recording,timestamps.get(i));
   words+=`<span class="caption-time" contenteditable="false">${link?`<a href="${esc(link)}" target="_blank" rel="noopener">▶ ${esc(unit.text)}</a>`:esc(unit.text)}</span>`;
  }else if(kind!=="speech") words+=markerMarkup(unit);
  else words+=wordMarkup(unit,recording,focus);
  if(unit.edit)i=unit.end-1;

 }
 const SPK=HAS("speakers"),CAP=HAS("captions");
 // The whole-item editor sits under a heading of its own (renderDetail), so it repeats none.
 const top=(index<0?"":`<b>${SPK?"Mark who is speaking":"Correct the captions"}</b>`)
  +(index<0&&!excerpts.length?"":`<button type="button" class="opt" data-expand>${spanFull[id]?"Back to passage":"Expand to full transcript"}</button>`);
 const view=hasPassage?"original passage":"this transcript";
 const marked=humanRanges(recording).filter(range=>range.start<hi&&range.end>lo&&speechIndices(data,Math.max(lo,range.start),Math.min(hi,range.end)).length);
 root.innerHTML=`${top?`<div class="span-top">${top}</div>`:""}
  <p class="span-instructions">Select words, then ${SPK&&CAP?"choose who said them or use Correct text for caption errors":SPK?"choose who said them":"use Correct text for caption errors"}. Caption gaps and playback times are skipped automatically.</p>
  <div class="span-legend">${SPEAKERS.map(sp=>`<span class="${speakerClass(sp.id)} reviewed">${esc(speakerLabel(sp.id,recording))} · reviewed</span>`).join("")}<span class="unclear reviewed">${esc(UNCLEAR.label)} · reviewed</span><span class="${speakerClass(SPEAKER_IDS[0])} draft">Dashed · unconfirmed suggestion</span><span>Plain · not reviewed</span>${focus?'<span class="evidence">Underline · passage being checked</span>':""}</div>
  ${data.suggestions_hidden?`<p class="span-model"><button type="button" class="opt" data-reveal ${writable?"":"disabled"}>Show suggestions (${data.suggestions_hidden})</button> Hidden until you ask, so what you mark first is your own reading. The ranges you have marked so far are kept as your blind answer.</p>`:""}
  <p class="span-model">${known?"Suggestions cover "+known+" of these "+spoken.length+" words. Confirm or correct them; their source is shown with each turn.":`${data.suggestions_hidden?"":"No suggestions for these words. "}Label the words you can identify; use “${esc(UNCLEAR.label)}” when you cannot identify the speaker.`} ${states.slice(lo,hi).some(s=>s.speaker==="conflict")?"Attributions conflict on the dotted words; review those yourself.":""}</p>
  ${index<0&&!spanFull[id]&&excerpts.length?`<label class="span-samples">Passage <select data-sample>${excerpts.map((e,i)=>`<option value="${i}" ${i===(spanSample[recording.key]||0)?"selected":""}>${Math.round(e.at*100)}% into the transcript</option>`).join("")}</select></label>`:""}
  <div class="span-actions"><div class="span-selection" aria-live="polite"></div><div class="opts">
   ${SPK?`${SPEAKERS.map(sp=>`<button class="opt ${speakerClass(sp.id)}" type="button" data-paint="${esc(sp.id)}">${esc(speakerLabel(sp.id,recording))} <kbd>${esc(sp.key)}</kbd></button>`).join("")}
   <button class="opt" type="button" data-paint="${esc(UNCLEAR.id)}">${esc(UNCLEAR.label)} <kbd>${esc(UNCLEAR.key)}</kbd></button>
   <button class="opt" type="button" data-paint="">Clear human marks</button>`:""}${CAP?'<button class="opt" type="button" data-correct-text>Correct text</button>':""}</div>
   <div class="span-boundaries" hidden><label>Selection start <input type="range" data-edge="start" aria-label="Selection start word"></label><label>Selection end <input type="range" data-edge="end" aria-label="Selection end word"></label></div>
  </div>
  ${correctionPanel(recording,index,lo,hi)}
  <div class="transcript-view" role="group" aria-label="Transcript display"><button type="button" class="opt" data-transcript-view="conversation" aria-pressed="${!spanSourceView[id]}">Conversation</button><button type="button" class="opt" data-transcript-view="source" aria-pressed="${!!spanSourceView[id]}">Source captions</button></div>
  ${spanSourceView[id]?'<p class="span-instructions">Original caption boundaries and labels. Your text corrections appear in both views.</p>':""}
  ${hasPassage?`<p class="span-instructions">Underlined words are being reviewed; other words provide context. Context starts 45 seconds before and after the original passage, keeping complete captions. Each button adds 45 seconds on that side, or 120 spoken words where timing is unavailable.</p>
  <div class="span-context" role="group" aria-label="Surrounding dialogue"><button type="button" class="opt" data-context-before ${lo===0?"disabled":""}>${lo===0?"Beginning of transcript":"Show more before"}</button><button type="button" class="opt" data-context-after ${hi===data.tokens.length?"disabled":""}>${hi===data.tokens.length?"End of transcript":"Show more after"}</button></div>`:""}
  <div class="span-transcript" tabindex="0" role="region" aria-label="Transcript passage for speaker annotation">${words}</div>
  ${hasPassage&&SPK?'<p class="span-instructions">Bulk confirmation covers only the original passage, including its saved context. Showing more dialogue does not widen it.</p>':""}
  <div class="opts">${SPK?`<button type="button" class="opt" data-confirm ${!writable||!markable?"disabled":""}>Confirm suggested spans in ${view}</button>`:""}
   <button type="button" class="opt" data-undo ${!writable||!spanUndo[recording.key]?.length?"disabled":""}>Undo marking</button>
   <button type="button" class="opt" data-retry ${!spanErrors[recording.key]?"hidden":""}>Retry save</button></div>
  <div class="span-status status" role="status"></div>
  <details class="span-ranges"><summary>${marked.length} reviewed span${marked.length===1?"":"s"} in view</summary>
   ${marked.map(range=>`<div class="span-range"><b>${esc(speakerLabel(range.speaker,recording))}</b> <span>${esc(speechText(data,range.start,Math.min(range.end,range.start+12)))}${range.end-range.start>12?"…":""}</span><button type="button" class="opt" data-select-span="${range.start},${range.end}">Select / adjust</button><button type="button" class="opt" data-remove-span="${range.start},${range.end}" ${writable?"":"disabled"}>Remove</button></div>`).join("")}
  </details>
  ${quote?reviewSummary(recording,index):""}`;

 const expand=root.querySelector("[data-expand]");
 if(expand)expand.onclick=()=>{spanFull[id]=!spanFull[id];drawSpanEditor(recording,index);};
 for(const side of ["before","after"]){
  const button=root.querySelector(`[data-context-${side}]`);
  if(button)button.onclick=()=>{
   expandSpanContext(recording,index,side);drawSpanEditor(recording,index);
   root.querySelector(`[data-context-${side}]`)?.focus({preventScroll:true});
  };
 }
 root.querySelectorAll("[data-transcript-view]").forEach(button=>button.onclick=()=>{spanSourceView[id]=button.dataset.transcriptView==="source";drawSpanEditor(recording,index);});
 root.querySelectorAll("[data-accept-turn]").forEach(button=>button.onclick=()=>{
  const [start,end]=button.dataset.acceptTurn.split(",").map(Number);
  acceptTurn(recording,index,start,end,button.dataset.speaker,button.dataset.source);
 });
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
   applyRevealedSuggestions(data,res);drawAllSpans(recording);
  }).catch(error=>{reveal.disabled=false;spanErrors[recording.key]=error.message||String(error);updateSpanStatus(recording);});
 };
 const sample=root.querySelector("[data-sample]");if(sample)sample.onchange=()=>{spanSample[recording.key]=+sample.value;delete spanSelections[id];drawSpanEditor(recording,index);};
 const area=root.querySelector(".span-transcript");
 restoreTranscriptPosition(area,position);
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
