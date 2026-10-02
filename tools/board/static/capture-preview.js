(() => {
  const el=id=>document.getElementById(id),say=value=>el("status").textContent=value;
  let payload=null,busy=false;
  function preview(){
    payload=null;el("save").disabled=true;el("cards").hidden=true;el("company").hidden=true;
    try{
      payload=JobFlowCapture.parse(el("payload").value);
      if(payload.kind==="company"){
        el("company").hidden=false;el("company-name").value=payload.company.name;el("company-url").value=payload.company.careers_url;el("confirmed").checked=false;
        el("company-links").replaceChildren();
        for(const url of payload.company.candidates){const button=document.createElement("button");button.textContent="Use detected board: "+url;button.onclick=()=>{el("company-url").value=url;el("confirmed").checked=false};el("company-links").append(button)}
        el("save").textContent="Follow company";
      }else{
        el("cards").hidden=false;el("count").textContent=payload.jobs.length+" job card"+(payload.jobs.length===1?"":"s");el("jobs").replaceChildren();
        for(const job of payload.jobs){const tr=document.createElement("tr");for(const value of [job.title||"ID "+job.job_id,job.company||"Unknown",[job.location,job.posted_date||job.posted,...job.badges].filter(Boolean).join(" · ")]){const td=document.createElement("td");td.textContent=value;tr.append(td)}if(job.description||job.apply_url){const details=document.createElement("details"),summary=document.createElement("summary"),body=document.createElement("p");summary.textContent="Additional captured details";body.textContent=[job.apply_url,job.description].filter(Boolean).join("\n\n");details.append(summary);details.append(body);tr.children[0].append(details)}el("jobs").append(tr)}
        el("save").textContent="Capture to inbox";
      }
      el("save").disabled=false;say("Review the preview, then press "+el("save").textContent+".");
    }catch(error){say(error.message)}
  }
  async function save(){
    if(!payload||busy)return;
    let token;try{token=localStorage.getItem("jobflow.token")}catch(_){}
    if(!token){say("Open JobFlow once using the address printed in the terminal, then return to this preview.");return}
    let body={jobs:payload.jobs},path="/api/linkedin/capture";
    try{
      if(payload.kind==="company"){
        if(!el("confirmed").checked)throw new Error("Confirm that the careers page belongs to this company first.");
        const value=JobFlowCapture.normalize({kind:"company",company:{name:el("company-name").value,careers_url:el("company-url").value}});
        body={...value.company,confirmed:true};path="/api/companies/capture";
      }
      busy=true;el("save").disabled=true;
      if(payload.kind==="company"){
        const response=await fetch("/api/companies?t="+encodeURIComponent(token));const data=await response.json();
        if(!response.ok)throw new Error(data.error||"Could not load companies.");body.mtime=data.mtime;
      }
      const response=await fetch(path+"?t="+encodeURIComponent(token),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}),data=await response.json();
      if(!response.ok)throw new Error(data.error||"Could not save this capture.");
      el("board").href="/?t="+encodeURIComponent(token)+(payload.kind==="company"?"#/companies":"#/inbox");
      const added=data.added||0;
      say(payload.kind==="company"?"Company saved. Open Companies to review its connection.":`${added} card${added===1?"":"s"} added; ${data.duplicates||0} already captured. Open the inbox to process descriptions.`);
      payload=null;
    }catch(error){say(error.message);el("save").disabled=false}finally{busy=false}
  }
  el("preview").addEventListener("click",preview);el("save").addEventListener("click",()=>void save());
  let raw="";try{if(location.hash.length<=JobFlowCapture.MAX_BYTES*3+1)raw=decodeURIComponent(location.hash.slice(1));else say("Capture exceeds 256 KB. Paste a smaller batch.")}catch(_){say("The capture fragment is malformed.")}
  history.replaceState(null,"",location.pathname);
  if(raw){el("payload").value=raw;preview();el("paste").open=!payload}
})();
