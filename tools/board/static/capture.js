// Shared by the board and the unauthenticated preview. This file carries no token.
(() => {
  const MAX_BYTES=262144,MAX_CARDS=250;
  const text=(value,max=1000)=>value==null?"":typeof value==="string"?value.slice(0,max):(()=>{throw new Error("Card metadata must be text.")})();
  function publicUrl(value){
    const raw=text(value,8193);let url;
    try{url=new URL(raw)}catch(_){throw new Error("Enter a complete http(s) URL.")}
    if(raw.length>8192||!/^(http|https):$/.test(url.protocol)||!url.hostname||url.username||url.password||/[\s\\\u0000-\u001f]/.test(raw))throw new Error("Enter a complete http(s) URL without credentials.");
    url.hash="";return url.href;
  }
  function parse(raw){
    if(typeof raw!=="string"||new TextEncoder().encode(raw).length>MAX_BYTES)throw new Error("Capture exceeds 256 KB. Capture a smaller batch.");
    let value;try{value=JSON.parse(raw)}catch(_){throw new Error("Paste a JSON capture payload.")}
    return normalize(value);
  }
  function isAtsUrl(value){
    try{const url=new URL(value);return /^(job-boards(?:\.eu)?\.greenhouse\.io|boards\.greenhouse\.io|jobs\.ashbyhq\.com|jobs(?:\.eu)?\.lever\.co|(?:careers|jobs)\.smartrecruiters\.com|[a-z0-9-]+\.jobs\.personio\.(?:de|com)|[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)$/.test(url.hostname)}catch(_){return false}
  }
  function normalize(value){
    if(value?.kind==="company"){
      const c=value.company||value,name=text(c.name,251).trim();
      if(!name||name.length>250)throw new Error("Enter a company name (up to 250 characters).");
      if(c.candidates!=null&&(!Array.isArray(c.candidates)||c.candidates.length>20))throw new Error("Too many careers-page links.");
      return {kind:"company",company:{name,careers_url:publicUrl(c.careers_url),candidates:(c.candidates||[]).map(publicUrl)}};
    }
    const jobs=Array.isArray(value)?value:value?.jobs;
    if(!Array.isArray(jobs)||!jobs.length||jobs.length>MAX_CARDS)throw new Error("Capture must contain 1–250 job cards.");
    const seen=new Set(),cards=[];
    for(const job of jobs){
      if(!job||typeof job!=="object"||Array.isArray(job))throw new Error("Each job card must be an object.");
      let id=String(job.job_id??job.id??"");const raw=job.linkedin_url||job.url;
      if(raw){
        const url=new URL(publicUrl(raw)),match=url.pathname.match(/^\/jobs\/view\/(\d+)\/?$/);
        if(!(url.hostname==="linkedin.com"||url.hostname.endsWith(".linkedin.com"))||!match)throw new Error("Job links must be LinkedIn /jobs/view/numeric-ID URLs.");
        if(id&&id!==match[1])throw new Error("Job ID and LinkedIn URL disagree.");
        id=match[1];
      }
      if(!/^\d{6,20}$/.test(id))throw new Error("Every card needs a numeric LinkedIn job ID (6–20 digits).");
      if(job.badges!=null&&(!Array.isArray(job.badges)||job.badges.length>12))throw new Error("Badges must be a small list of text labels.");
      const card={job_id:id,linkedin_url:"https://www.linkedin.com/jobs/view/"+id,title:text(job.title,500),company:text(job.company,300),location:text(job.location,500),posted:text(job.posted||job.posted_text,100),posted_date:text(job.posted_date,100),badges:(job.badges||[]).map(v=>text(v,100))};
      if(job.description)card.description=text(job.description,60000);
      if(job.apply_url||job.applyUrl)card.apply_url=publicUrl(job.apply_url||job.applyUrl);
      if(!seen.has(id)){seen.add(id);cards.push(card)}
    }
    return {kind:"linkedin",jobs:cards};
  }
  function linkedinBookmarklet(origin){
    const code=`(()=>{if(!/(^|\\.)linkedin\\.com$/.test(location.hostname)){alert("Open a LinkedIn jobs list first.");return}const out=new Map(),q=(n,s)=>n.querySelector(s)?.innerText?.trim()||"";for(const a of document.querySelectorAll('a[href*="/jobs/view/"]')){const m=a.href.match(/\\/jobs\\/view\\/(\\d+)/);if(!m)continue;const n=a.closest('[data-occludable-job-id],li,.job-card-container')||a.parentElement;if(!n||!n.getClientRects().length)continue;const id=m[1],previous=out.get(id),card={job_id:id,linkedin_url:"https://www.linkedin.com/jobs/view/"+id,title:q(n,'.job-card-list__title--link,.job-card-list__title,.base-search-card__title')||a.innerText.trim(),company:q(n,'.artdeco-entity-lockup__subtitle,.job-card-container__primary-description,.base-search-card__subtitle'),location:q(n,'.artdeco-entity-lockup__caption,.job-card-container__metadata-item,.job-search-card__location'),posted:q(n,'time,.job-search-card__listdate,.job-search-card__listdate--new'),posted_date:n.querySelector('time')?.getAttribute('datetime')||"",badges:[...n.querySelectorAll('.job-card-container__footer-item,.job-card-list__footer-wrapper li')].map(x=>x.innerText.trim()).filter(Boolean).map(v=>v.slice(0,100)).slice(0,12)};if(previous){for(const key of ["title","company","location","posted","posted_date"])card[key]=previous[key]||card[key];card.badges=[...new Set([...previous.badges,...card.badges])].slice(0,12)}out.set(id,card);if(out.size>=250)break}if(!out.size){alert("No rendered LinkedIn job cards found. Scroll to the list and try again.");return}const raw=JSON.stringify({jobs:[...out.values()]});if(new TextEncoder().encode(raw).length>262144){alert("Capture too large. Capture a smaller list.");return}window.open(${JSON.stringify(origin+"/static/capture.html#")}+encodeURIComponent(raw),"_blank","noopener")})()`;
    return "javascript:"+code;
  }
  function companyBookmarklet(origin){
    const code=`(()=>{const candidates=[...new Set([...document.querySelectorAll('a[href],iframe[src]')].map(a=>a.href||a.src).filter(u=>{try{return /^(job-boards(?:\\.eu)?\\.greenhouse\\.io|boards\\.greenhouse\\.io|jobs\\.ashbyhq\\.com|jobs(?:\\.eu)?\\.lever\\.co|(?:careers|jobs)\\.smartrecruiters\\.com|[a-z0-9-]+\\.jobs\\.personio\\.(?:de|com)|[a-z0-9-]+\\.wd\\d+\\.myworkdayjobs\\.com|wd\\d+\\.myworkdaysite\\.com)$/.test(new URL(u).hostname)}catch(_){return false}}))].slice(0,20);const raw=JSON.stringify({kind:"company",company:{name:document.querySelector('meta[property="og:site_name"]')?.content||document.querySelector('h1')?.innerText.trim()||document.title,careers_url:location.href,candidates}});window.open(${JSON.stringify(origin+"/static/capture.html#")}+encodeURIComponent(raw),"_blank","noopener")})()`;
    return "javascript:"+code;
  }
  globalThis.JobFlowCapture={parse,normalize,publicUrl,isAtsUrl,linkedinBookmarklet,companyBookmarklet,MAX_BYTES};
})();
