// Shared by the web rail, Sheets sidebar and Excel task pane. Cell values never cross this boundary.
(function(root){
  'use strict';
  function header(csv){
    const columns=[];let value='',quoted=false;
    for(let i=0;i<String(csv||'').length;i++){
      const c=csv[i];
      if(c==='"'){
        if(quoted&&csv[i+1]==='"'){value+='"';i++;}else quoted=!quoted;
      }else if(!quoted&&(c===','||c==='\n'||c==='\r')){
        columns.push(value);value='';if(c!==',')return columns;
      }else value+=c;
    }
    columns.push(value);return columns;
  }
  function schema(tables,active){
    const sheets=tables.map(t=>({name:t.name,columns:header(t.data)}));
    return {sheets,active_sheet:sheets.some(t=>t.name===active)?active:sheets[0]?.name,scope:sheets.map(t=>t.name)};
  }
  function merge(metadata,tables,active){
    const normalized=schema(tables||[],active||metadata?.active_sheet);
    const sheets=(metadata?.sheets||[]).map(sheet=>normalized.sheets.find(t=>t.name===sheet.name)||sheet);
    for(const sheet of normalized.sheets)if(!sheets.some(t=>t.name===sheet.name))sheets.push(sheet);
    const scope=normalized.sheets.length?normalized.scope:metadata?.scope||[];
    if(!scope.length||scope.some(name=>!sheets.find(t=>t.name===name)?.columns.length))return null;
    const current=active||metadata?.active_sheet||normalized.active_sheet;
    return {sheets,scope,active_sheet:sheets.some(t=>t.name===current)?current:sheets[0]?.name};
  }
  function fallback(metadata){
    const active=metadata.active_sheet;
    const sheet=metadata.sheets.find(item=>item.name===active&&metadata.scope.includes(item.name))||
      metadata.sheets.find(item=>metadata.scope.includes(item.name));
    if(!sheet||!sheet.columns.length)return [];
    const table=JSON.stringify(sheet.name),field=JSON.stringify(sheet.columns[0]);
    const questions=[`How many records are in ${table}?`,`Which values are most common in ${field} in ${table}?`];
    if(sheet.columns.length>1)questions.push(`How does ${JSON.stringify(sheet.columns[0])} vary across ${JSON.stringify(sheet.columns[1])} in ${table}?`);
    else questions.push(`Show the records in ${table} with the highest ${field}.`);
    return questions;
  }
  function create({container,composer,request}){
    let generation=0,key='',loaded=[];
    container.classList.add('starter-questions');
    container.setAttribute('aria-label','Suggested questions');
    function clear(){generation++;key='';loaded=[];container.replaceChildren();container.hidden=true;}
    function render(questions,pending){
      container.replaceChildren();container.hidden=!pending&&!questions.length;
      if(!pending&&!questions.length)return;
      const title=document.createElement('div');title.className='starter-title';title.textContent='Questions for this sheet';container.append(title);
      if(pending){const status=document.createElement('div');status.className='starter-status';status.setAttribute('role','status');status.textContent='Finding useful questions…';container.append(status);}
      for(const question of questions){
        const button=document.createElement('button');button.type='button';button.className='starter-question';
        button.textContent=question;
        button.addEventListener('click',()=>{
          // A click offers wording; it never executes or replaces a different draft without a choice.
          if(composer.value.trim()&&composer.value.trim()!==question){
            const insert=document.createElement('button');insert.type='button';insert.className='starter-question';
            insert.textContent='Replace my draft with this question';
            insert.addEventListener('click',()=>{composer.value=question;composer.dispatchEvent(new Event('input',{bubbles:true}));composer.focus();render(loaded);});
            container.replaceChildren(button,insert);return;
          }
          composer.value=question;composer.dispatchEvent(new Event('input',{bubbles:true}));composer.focus();
        });
        container.append(button);
      }
    }
    async function update(metadata){
      if(!metadata?.sheets?.length){clear();return;}
      const next=JSON.stringify(metadata);if(next===key)return;
      const own=++generation;key=next;loaded=[];render([],true);
      try{
        const result=await request(metadata);
        if(own!==generation)return;
        loaded=(Array.isArray(result?.questions)?result.questions:[]).filter(q=>typeof q==='string'&&q.trim()&&q.length<=240).slice(0,3);
        if(loaded.length<3)loaded=fallback(metadata);
        render(loaded);
      }catch(_){if(own===generation){loaded=fallback(metadata);render(loaded);}}
    }
    return {update,clear};
  }
  root.PrereasonerSuggestions={create,schema,merge,header,fallback};
  if(typeof module==='object'&&module.exports)module.exports=root.PrereasonerSuggestions;
})(globalThis);
