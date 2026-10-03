// Stable Apps Script shell: all UI markup, styles and behavior are hosted and updated together.
(async function(){
  const base='https://chat.prereasoner.com/office/sheets/';
  try{
    const response=await fetch(base+window.PrereasonerShell.page+'.html',{cache:'no-cache',signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error('UI unavailable');
    const page=new DOMParser().parseFromString(await response.text(),'text/html');
    const scripts=[...page.querySelectorAll('script')];scripts.forEach(script=>script.remove());
    const draft=document.getElementById('question')?.value||'';
    page.querySelectorAll('link[rel=stylesheet]').forEach(link=>document.head.append(link.cloneNode(true)));
    document.body.replaceChildren(...page.body.childNodes);
    const question=document.getElementById('question');if(question)question.value=draft;
    for(const source of scripts){
      await new Promise((resolve,reject)=>{
        const script=document.createElement('script');
        if(source.type)script.type=source.type;
        script.src=source.src;script.onload=resolve;script.onerror=reject;
        document.body.append(script);
      });
    }
  }catch(_){
    const status=document.createElement('p');status.role='status';status.textContent='Prereasoner could not load. Close and reopen the sidebar to retry.';
    document.body.prepend(status);
  }
})();
