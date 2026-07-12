(function(){
  'use strict';
  var nativeFetch = window.fetch.bind(window);
  var state = {enabled:false, open:false, data:null, destination:'', clearArmed:false, showHistory:false, timer:null};
  try{localStorage.removeItem('atelierAgentBatchV2');}catch(e){}

  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  function shortPath(s){ s=String(s||''); var p=s.split('/'); return p.length>2?'…/'+p.slice(-2).join('/'):s; }
  function sameOriginPath(input){
    try{
      var raw=typeof input==='string'?input:(input&&input.url)||'';
      var u=new URL(raw,location.href);
      return u.origin===location.origin?u.pathname:'';
    }catch(e){ return ''; }
  }
  function enrich(options){
    options=Object.assign({},options||{});
    var headers=options.headers||{};
    var contentType=(headers['Content-Type']||headers['content-type']||'').toLowerCase();
    if(!options.body || contentType.indexOf('application/json')<0) return options;
    try{
      var body=JSON.parse(options.body);
      delete body.destination;
      delete body.batchId;
      var sendNow=body.deliveryMode==='direct'||body.held===false||body.action==='apply';
      delete body.deliveryMode;
      body.action=sendNow?'apply':'ask';
      body.held=!sendNow;
      options.body=JSON.stringify(body);
    }catch(e){}
    return options;
  }
  window.fetch=function(input,options){
    var path=sameOriginPath(input);
    var tracked=state.enabled && ['/quote','/save','/agent-selection'].indexOf(path)>=0 &&
      String((options&&options.method)||'GET').toUpperCase()==='POST';
    var next=tracked?enrich(options):options;
    return nativeFetch(input,next).then(function(response){
      if(tracked){
        response.clone().json().then(function(j){
          if(j&&j.queuedForAgent){
            toast(j.agentSelectionStatus==='staged'?'Ajoutée aux annotations':'Envoyée à Codex');
            refreshSoon();
          }
        }).catch(function(){});
      }
      return response;
    });
  };

  var style=document.createElement('style');
  style.textContent='\
  #atelierAgentHub{--aa-bg:#292c33;--aa-raised:#353840;--aa-hover:#30333a;--aa-text:rgba(255,255,255,.88);--aa-muted:rgba(255,255,255,.48);--aa-faint:rgba(255,255,255,.30);--aa-line:rgba(255,255,255,.08);position:fixed;right:14px;bottom:14px;z-index:2147483000;font:12px/1.35 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;color:var(--aa-text);-webkit-font-smoothing:antialiased}\
  #atelierAgentButton{appearance:none!important;position:relative;width:32px!important;height:32px!important;min-width:32px!important;padding:0!important;display:grid!important;place-items:center!important;border:0!important;border-radius:0!important;background:transparent!important;box-shadow:none!important;color:var(--aa-muted)!important;cursor:pointer;transition:transform .12s ease,color .12s ease}\
  @media(hover:hover){#atelierAgentButton:hover{background:transparent!important;color:var(--aa-text)!important}.aabtn:hover{background:rgba(255,255,255,.055);color:var(--aa-text)}}\
  #atelierAgentButton:active,.aabtn:active{transform:scale(.96)}#atelierAgentButton:focus-visible{outline:0;color:var(--aa-text)!important} .aabtn:focus-visible,.aaselect:focus-visible{outline:1px solid rgba(255,255,255,.38);outline-offset:2px}\
  .aadot{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.35;stroke-linecap:round;stroke-linejoin:round;color:rgba(255,255,255,.34)}.aadot.online{color:rgba(255,255,255,.62)}.aadot.busy{color:rgba(255,255,255,.86);animation:aapulse 1.2s ease-in-out infinite}.aacount{position:absolute;left:21px;top:2px;color:rgba(255,255,255,.62);font-size:9px;font-variant-numeric:tabular-nums}\
  @keyframes aapulse{50%{opacity:.36;transform:scale(.78)}} @media(prefers-reduced-motion:reduce){.aadot.busy{animation:none}}\
  #atelierAgentPanel{position:absolute;right:0;bottom:40px;width:min(318px,calc(100vw - 20px));max-height:min(480px,calc(100vh - 72px));overflow:auto;padding:11px 12px;border:0;border-radius:0;background:var(--aa-bg);box-shadow:none;display:none;overscroll-behavior:contain}\
  #atelierAgentPanel.open{display:block}.aasummary{display:flex;align-items:baseline;justify-content:space-between;gap:12px;min-height:22px}.aatitle{color:var(--aa-text);font-weight:500}.aaconnection{max-width:170px;color:var(--aa-muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
  .aaqueue{margin-top:7px}.aaannotation{display:grid;grid-template-columns:minmax(0,1fr) 28px 28px;align-items:center;gap:2px;min-height:51px;padding:5px 0;border-top:1px solid rgba(255,255,255,.055)}.aaannotation:first-child{border-top:0}.aacopy{min-width:0}.aafile{color:rgba(255,255,255,.75);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.aapreview{margin-top:3px;color:var(--aa-faint);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
  .aaiconbtn{appearance:none;width:28px;height:28px;display:grid;place-items:center;padding:0;border:0;border-radius:0;background:transparent;color:var(--aa-muted);cursor:pointer;transition:transform .12s ease,color .12s ease}.aaiconbtn svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.25;stroke-linecap:round;stroke-linejoin:round}.aaiconbtn:active{transform:scale(.9)}.aaiconbtn:focus-visible{outline:1px solid rgba(255,255,255,.38);outline-offset:0}.aaiconbtn:disabled{color:rgba(255,255,255,.16);cursor:default}@media(hover:hover){.aaiconbtn:hover:not(:disabled){color:var(--aa-text)}}\
  .aaempty{padding:19px 0 13px;color:var(--aa-faint);font-size:11px;text-align:center}.aastatus{color:var(--aa-faint);font-size:9px;margin-top:2px}.aafooter{display:flex;justify-content:flex-end;gap:14px;padding-top:8px;border-top:1px solid rgba(255,255,255,.055)}.aalink{appearance:none;padding:2px 0;border:0;background:transparent;color:var(--aa-muted);font:inherit;font-size:10px;cursor:pointer}.aalink:disabled{opacity:.38;cursor:default}@media(hover:hover){.aalink:hover:not(:disabled){color:var(--aa-text)}}\
  .aatoast{position:fixed;right:14px;bottom:58px;padding:8px 11px;border:1px solid var(--aa-line);border-radius:8px;background:var(--aa-raised);color:var(--aa-text);box-shadow:0 10px 28px rgba(0,0,0,.28);opacity:0;transform:translateY(6px);transition:opacity .16s ease,transform .16s ease;pointer-events:none}.aatoast.show{opacity:1;transform:none}\
  body>.brand #atelierAgentHub,header #atelierAgentHub{position:static;margin-left:2px}body>.brand #atelierAgentPanel,header #atelierAgentPanel{position:fixed;right:16px;top:52px;bottom:auto}\
  @media(max-width:520px){#atelierAgentPanel{right:-4px;width:min(300px,calc(100vw - 16px))}}';
  document.head.appendChild(style);

  var hub=document.createElement('div'); hub.id='atelierAgentHub';
  hub.innerHTML='<button id="atelierAgentButton" type="button" aria-label="Annotations à envoyer" aria-expanded="false" aria-controls="atelierAgentPanel"><svg class="aadot" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3.5h10v7H8.25L5 13v-2.5H3z"/><path d="M5.5 6.8h5"/></svg><span class="aacount"></span></button><section id="atelierAgentPanel" aria-label="Annotations à envoyer"><div class="aasummary"><span class="aatitle" id="aaTitle">Annotations</span><span class="aaconnection" id="aaConnection">Aucun chat connecté</span></div><div class="aaqueue" id="aaQueue"></div><div class="aafooter"><button class="aalink" id="aaHistory" type="button">Historique</button><button class="aalink" id="aaDeleteAll" type="button">Tout supprimer</button><button class="aalink" id="aaSendAll" type="button">Tout envoyer</button></div></section><div class="aatoast" role="status" aria-live="polite"></div>';
  var brand=document.querySelector('.brand'), spacer=brand&&brand.querySelector('.brand-sp');
  if(spacer) brand.insertBefore(hub,spacer); else document.body.appendChild(hub);
  var button=hub.querySelector('#atelierAgentButton'), panel=hub.querySelector('#atelierAgentPanel');
  var bubbleDragged=false;
  if(window.self!==window.top){
    button.style.cursor='grab';
    try{
      var savedBubble=JSON.parse(localStorage.getItem('atelierAgentBubblePos')||'null');
      if(savedBubble&&Number.isFinite(savedBubble.x)&&Number.isFinite(savedBubble.y)){
        hub.style.left=Math.max(4,Math.min(innerWidth-36,savedBubble.x))+'px';
        hub.style.top=Math.max(4,Math.min(innerHeight-36,savedBubble.y))+'px';
        hub.style.right='auto';hub.style.bottom='auto';
      }
    }catch(e){}
    button.addEventListener('pointerdown',function(e){
      if(e.button!==0)return;
      button.style.cursor='grabbing';
      var startX=e.clientX,startY=e.clientY,r=hub.getBoundingClientRect(),originX=r.left,originY=r.top,moved=false;
      button.setPointerCapture(e.pointerId);
      function move(ev){
        var dx=ev.clientX-startX,dy=ev.clientY-startY;
        if(Math.abs(dx)+Math.abs(dy)>4)moved=true;
        if(!moved)return;
        hub.style.left=Math.max(4,Math.min(innerWidth-r.width-4,originX+dx))+'px';
        hub.style.top=Math.max(4,Math.min(innerHeight-r.height-4,originY+dy))+'px';
        hub.style.right='auto';hub.style.bottom='auto';
      }
      function up(ev){
        button.style.cursor='grab';
        button.removeEventListener('pointermove',move);button.removeEventListener('pointerup',up);
        if(moved){
          bubbleDragged=true;
          var end=hub.getBoundingClientRect();
          try{localStorage.setItem('atelierAgentBubblePos',JSON.stringify({x:end.left,y:end.top}));}catch(err){}
          setTimeout(function(){bubbleDragged=false;},0);
        }
      }
      button.addEventListener('pointermove',move);button.addEventListener('pointerup',up);
    });
  }
  function closePanel(){
    state.open=false;panel.classList.remove('open');button.setAttribute('aria-expanded','false');
  }
  if(window.self===window.top){
    window.addEventListener('message',function(e){
      if(e.origin===location.origin&&e.data&&e.data.type==='atelier-close-agent-panel'&&state.open)closePanel();
    });
  }else{
    var closeParentPanel=function(){
      try{
        var parentPanel=window.parent.document.getElementById('atelierAgentPanel');
        var parentButton=window.parent.document.getElementById('atelierAgentButton');
        if(parentPanel&&parentPanel.classList.contains('open')&&parentButton)parentButton.click();
      }catch(e){window.parent.postMessage({type:'atelier-close-agent-panel'},location.origin);}
    };
    document.addEventListener('pointerdown',closeParentPanel,true);
    document.addEventListener('click',closeParentPanel,true);
  }
  button.onclick=function(){if(bubbleDragged)return;state.open=!state.open;panel.classList.toggle('open',state.open);button.setAttribute('aria-expanded',String(state.open));if(state.open)refresh();};
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&state.open)closePanel();});
  document.addEventListener('pointerdown',function(e){
    if(state.open&&!hub.contains(e.target))closePanel();
  },true);
  window.addEventListener('blur',function(){
    setTimeout(function(){
      if(state.open&&document.activeElement&&document.activeElement.tagName==='IFRAME')closePanel();
    },0);
  });
  function mutate(path,ids,destination){
    var body={ids:ids};if(destination)body.destination=destination;
    return nativeFetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){if(!r.ok||j.error)throw new Error(j.error||('HTTP '+r.status));return j;});})
      .then(function(j){refreshSoon();return j;});
  }
  function bankItems(){return ((state.data&&state.data.pending)||[]).filter(function(i){return i.status==='staged'||i.held;});}
  panel.addEventListener('click',function(e){
    var send=e.target.closest('[data-send]'),del=e.target.closest('[data-delete]'),restore=e.target.closest('[data-restore]');
    if(send){
      if(!state.destination)return toast('Lance /atelier dans le chat cible');
      mutate('/agent-annotations/release',[send.dataset.send],state.destination).then(function(){toast('Annotation envoyée');}).catch(function(err){toast(err.message);});
    }else if(del){
      mutate('/agent-annotations/delete',[del.dataset.delete]).then(function(){toast('Annotation supprimée');}).catch(function(err){toast(err.message);});
    }else if(restore){
      mutate('/agent-annotations/restore',[restore.dataset.restore]).then(function(){state.showHistory=false;toast('Annotation restaurée');}).catch(function(err){toast(err.message);});
    }
  });
  hub.querySelector('#aaHistory').onclick=function(){state.showHistory=!state.showHistory;render();};
  hub.querySelector('#aaSendAll').onclick=function(){
    var ids=bankItems().map(function(i){return i.id;});if(!ids.length)return;
    if(!state.destination)return toast('Lance /atelier dans le chat cible');
    mutate('/agent-annotations/release',ids,state.destination).then(function(){toast(ids.length+' annotation'+(ids.length>1?'s':'')+' envoyée'+(ids.length>1?'s':''));}).catch(function(err){toast(err.message);});
  };
  hub.querySelector('#aaDeleteAll').onclick=function(){
    var ids=bankItems().map(function(i){return i.id;});if(!ids.length)return;
    if(!state.clearArmed){
      state.clearArmed=true;this.textContent='Confirmer la suppression';
      setTimeout(function(){state.clearArmed=false;render();},3500);return;
    }
    state.clearArmed=false;
    mutate('/agent-annotations/delete',ids).then(function(){toast(ids.length+' annotation'+(ids.length>1?'s':'')+' supprimée'+(ids.length>1?'s':''));}).catch(function(err){toast(err.message);});
  };

  function currentConsumer(){return state.data&&(state.data.consumers||[]).find(function(c){return c.id===state.destination;});}
  function render(){
    if(!state.data)return;
    var consumers=state.data.consumers||[];
    // Une tâche Codex peut ne pas avoir émis de heartbeat depuis 180 s tout en
    // restant la bonne destination. Dans ce cas, on y met l'annotation en file
    // plutôt que de désactiver toutes les actions d'envoi.
    var latest=consumers.find(function(c){return c.online;})||consumers[0]||null;
    state.destination=latest?latest.id:'';
    var pending=state.data.pending||[],bank=bankItems(),cur=currentConsumer(),history=(state.data.history||[]).slice(0,30);
    hub.querySelector('#aaTitle').textContent=state.showHistory?'Historique':('Annotations'+(bank.length?' · '+bank.length:''));
    hub.querySelector('#aaConnection').textContent=cur
      ?('Vers '+(cur.label||'ce chat')+(cur.online?'':' · en attente'))
      :'Lance /atelier dans le chat cible';
    var cross='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8"/></svg>';
    var plane='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m2.8 7.6 10-4.2-3.9 10-1.5-4.2-4.6-1.6ZM7.4 9.2l2.1-2.1"/></svg>';
    var source=state.showHistory?history:bank;
    hub.querySelector('#aaQueue').innerHTML=source.length?source.map(function(i){
      var location=i.page?(' · '+i.page):'',preview=i.comment||i.selection||((i.notes||[]).map(function(n){return n.text;}).join(' · '))||'Annotation visuelle';
      var actions=state.showHistory?(i.status==='cancelled'?'<span></span><button class="aaiconbtn" type="button" data-restore="'+esc(i.id)+'" aria-label="Restaurer cette annotation" title="Restaurer"><svg viewBox="0 0 16 16"><path d="M5 5H2.5v-2.5M2.8 5a5.4 5.4 0 1 1-.2 5"/></svg></button>':'<span></span><span></span>'):'<button class="aaiconbtn" type="button" data-delete="'+esc(i.id)+'" aria-label="Supprimer cette annotation" title="Supprimer">'+cross+'</button><button class="aaiconbtn" type="button" data-send="'+esc(i.id)+'" aria-label="Envoyer cette annotation à ce chat" title="Envoyer à ce chat"'+(state.destination?'':' disabled')+'>'+plane+'</button>';
      return '<div class="aaannotation"><div class="aacopy"><div class="aafile">'+esc(shortPath(i.path||i.original||'Annotation'))+esc(location)+'</div><div class="aapreview">'+esc(preview)+'</div>'+(state.showHistory?'<div class="aastatus">'+esc(i.status||'')+'</div>':'')+'</div>'+actions+'</div>';
    }).join(''):'<div class="aaempty">'+(state.showHistory?'Aucun historique':'Aucune annotation en attente')+'</div>';
    hub.querySelector('#aaHistory').textContent=state.showHistory?'Retour':'Historique';
    hub.querySelector('#aaDeleteAll').disabled=!bank.length;
    hub.querySelector('#aaDeleteAll').style.display=state.showHistory?'none':'';
    hub.querySelector('#aaDeleteAll').textContent=state.clearArmed?'Confirmer la suppression':'Tout supprimer';
    hub.querySelector('#aaSendAll').disabled=!bank.length||!state.destination;
    hub.querySelector('#aaSendAll').style.display=state.showHistory?'none':'';
    var busy=bank.length>0;
    var dot=hub.querySelector('.aadot');dot.setAttribute('class','aadot '+(busy?'busy':(cur&&cur.online?'online':'')));
    hub.querySelector('.aacount').textContent=bank.length?String(bank.length):'';
  }
  function refresh(){
    nativeFetch('/agent-status?limit=40').then(function(r){return r.json();}).then(function(j){
      // The movable bank belongs to the editor surface and remains available
      // even before a chat connects. Keep the top-level gallery clean unless a
      // live Codex host explicitly enables its bank there.
      var editorSurface=window.self!==window.top||!document.querySelector('.brand');
      var visible=j&&(editorSurface||j.agentHost==='codex');
      if(visible){state.enabled=true;state.data=j;hub.style.display='block';render();window.dispatchEvent(new CustomEvent('atelier-agent-status',{detail:j}));}
      else hub.style.display='none';
    }).catch(function(){hub.style.display='none';});
  }
  function refreshSoon(){setTimeout(refresh,120);setTimeout(refresh,900);}
  var toastTimer=null;function toast(text){var el=hub.querySelector('.aatoast');el.textContent=text;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(function(){el.classList.remove('show');},2200);}
  window.AtelierAgentContext={enrich:function(payload){payload=Object.assign({},payload||{});delete payload.destination;delete payload.batchId;payload.action='ask';payload.held=true;return payload;},refresh:refresh,getState:function(){return state;}};
  hub.style.display='none';refresh();state.timer=setInterval(refresh,1800);
})();
