/* sel_overlay.js — select text in a project HTML report → annotate → send to Claude.
   Injected by fig_annotate_server into project .html files (never the gallery index
   or the /.fig_thumbs viewers, which have their own selection systems).
   Selecting text: (1) feeds ~/.claude/fig-selection.json via /selinfo (the "ma
   sélection" flow), (2) shows a pill near the selection: [💬 Annoter] [↑ send].
   ↑ sends `path : « text »` (+ optional comment) into the Claude surface and
   auto-submits (/quote with direct:true). No animations by design. */
(function(){
  if (window.__claudeSelOverlay) return; window.__claudeSelOverlay = true;
  var REL = decodeURIComponent(location.pathname.replace(/^\//,''));
  var NAME = REL.split('/').pop();

  var css = '#csel-pill{position:fixed;z-index:2147483000;display:none;align-items:center;gap:8px;'
    +'background:rgba(24,27,34,.97);border:1px solid #3a4150;border-radius:22px;padding:5px 6px 5px 12px;'
    +'box-shadow:0 10px 36px rgba(0,0,0,.45);color:#e4e4e7;'
    +'font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;user-select:none;-webkit-user-select:none}'
    +'#csel-pill .n{color:#9aa3b2;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    +'#csel-pill button{border:none;cursor:pointer;display:flex;align-items:center;justify-content:center}'
    +'#csel-pill .go{width:32px;height:32px;border-radius:50%;background:#5b9dff;color:#fff;font-size:16px;flex:none}'
    +'#csel-pill .go:hover{background:#76adff}'
    +'#csel-pill .cm{height:26px;padding:0 10px;border-radius:14px;background:transparent;'
    +'border:1px solid #3a4150;color:#e4e4e7;font-size:12px;flex:none}'
    +'#csel-pill .cm:hover{border-color:#5b6575}'
    +'#csel-pill .x{width:28px;height:28px;border-radius:50%;background:transparent;'
    +'border:1px solid #3a4150;color:#9aa3b2;font-size:13px;flex:none}'
    +'#csel-pill .x:hover{border-color:#5b6575;color:#fff}'
    +'#csel-card{position:fixed;z-index:2147483001;display:none;flex-direction:column;gap:10px;width:340px;'
    +'background:rgba(24,27,34,.97);border:1px solid #3a4150;border-radius:16px;padding:12px 14px;'
    +'box-shadow:0 14px 48px rgba(0,0,0,.55);font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#e4e4e7}'
    +'#csel-card textarea{background:rgba(255,255,255,.04);border:1px solid #3a4150;border-radius:10px;'
    +'outline:none;color:#e4e4e7;font-size:13px;line-height:1.45;padding:8px 10px;resize:none;'
    +'font-family:inherit;min-height:44px;width:100%;box-sizing:border-box}'
    +'#csel-card textarea:focus{border-color:#5b9dff;box-shadow:0 0 0 2px rgba(91,157,255,.22)}'
    +'#csel-card .b{display:flex;gap:8px;justify-content:flex-end}'
    +'#csel-card button{padding:6px 14px;font-size:12.5px;border-radius:8px;cursor:pointer;'
    +'border:1px solid #3a4150;background:transparent;color:#e4e4e7}'
    +'#csel-card .cc:hover{border-color:#5b6575}'
    +'#csel-card .sv{background:#e8eaed;border-color:#e8eaed;color:#111;font-weight:600}'
    +'#csel-card .sv:hover{background:#fff}';
  var st = document.createElement('style'); st.textContent = css;
  document.head.appendChild(st);

  var pill = document.createElement('div'); pill.id = 'csel-pill';
  pill.innerHTML = '<span class="n"></span>'
    + '<button class="cm" title="Ajouter un commentaire puis envoyer">\u{1F4AC} Annoter</button>'
    + '<button class="x" title="Annuler : désélectionner sans envoyer (Esc)">✕</button>'
    + '<button class="go" title="Envoyer la sélection à la session Claude">↑</button>';
  document.body.appendChild(pill);

  var card = document.createElement('div'); card.id = 'csel-card';
  card.innerHTML = '<textarea rows="2" placeholder="Ajouter une annotation…"></textarea>'
    + '<div class="b"><button class="cc">Annuler</button><button class="sv">Enregistrer</button></div>';
  document.body.appendChild(card);

  var selText = '', selRect = null, tmr = 0;

  function place(el, rect){
    el.style.display = 'flex';
    var w = el.offsetWidth, h = el.offsetHeight;
    var x = Math.min(Math.max(8, rect.left + rect.width/2 - w/2), innerWidth - w - 8);
    var y = rect.bottom + 10;
    if (y + h > innerHeight - 8) y = rect.top - h - 10;
    el.style.left = x + 'px'; el.style.top = Math.max(8, y) + 'px';
  }
  function hideAll(){ pill.style.display = 'none'; card.style.display = 'none'; }

  function pushSel(text){
    try{
      fetch('/selinfo', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(text
          ? {text: text.slice(0, 4000), rel: REL, name: NAME, page: 'html',
             lines: 1, words: text.split(/\s+/).length}
          : {lines: 0, words: 0})});
    }catch(e){}
  }

  document.addEventListener('selectionchange', function(){
    clearTimeout(tmr);
    tmr = setTimeout(function(){
      if (card.style.display === 'flex') return;   // typing a comment: freeze the pill
      var s = window.getSelection(), t = s ? String(s).trim() : '';
      if (!t || !s.rangeCount){ selText = ''; hideAll(); pushSel(''); return; }
      selText = t;
      selRect = s.getRangeAt(0).getBoundingClientRect();
      pill.querySelector('.n').textContent = t.length > 60 ? t.slice(0, 57).replace(/\s+\S*$/, '') + '…' : t;
      pushSel(t);
      place(pill, selRect);
    }, 180);
  });

  // clicks on the pill must not destroy the selection
  pill.addEventListener('mousedown', function(e){ e.preventDefault(); });

  function send(comment){
    var go = pill.querySelector('.go');
    go.textContent = '⏳';
    fetch('/quote', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rel: REL, page: '', text: selText, comment: comment || '', direct: true})})
      .then(function(r){ return r.json(); })
      .then(function(){ go.textContent = '✓'; setTimeout(function(){ go.textContent = '↑'; hideAll(); }, 1200); })
      .catch(function(){ go.textContent = '!'; setTimeout(function(){ go.textContent = '↑'; }, 1600); });
  }

  function cancel(){
    selText = '';
    try{ window.getSelection().removeAllRanges(); }catch(e){}
    hideAll();
    pushSel('');
  }

  pill.querySelector('.go').addEventListener('click', function(){ send(''); });
  pill.querySelector('.x').addEventListener('click', function(e){ e.stopPropagation(); cancel(); });
  pill.querySelector('.cm').addEventListener('click', function(){
    place(card, selRect);
    card.querySelector('textarea').focus();
  });
  card.querySelector('.cc').addEventListener('click', function(){
    card.style.display = 'none'; card.querySelector('textarea').value = '';
  });
  card.querySelector('.sv').addEventListener('click', function(){
    var v = card.querySelector('textarea').value.trim();
    card.style.display = 'none'; card.querySelector('textarea').value = '';
    send(v);
  });
  card.querySelector('textarea').addEventListener('keydown', function(e){
    e.stopPropagation();
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); card.querySelector('.sv').click(); }
    else if (e.key === 'Escape'){ card.querySelector('.cc').click(); }
  });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') cancel(); });

  /* ---- drawn-annotation mode (shared AnnotKit over the whole document) ---- */
  var akBtnCss = '#csel-annot-btn{position:fixed;right:16px;bottom:16px;z-index:901;width:40px;height:40px;'
    +'border-radius:50%;border:1px solid #3a4150;background:rgba(24,27,34,.97);color:#e4e4e7;'
    +'font-size:18px;line-height:1;cursor:pointer;box-shadow:0 8px 28px rgba(0,0,0,.45);'
    +'display:flex;align-items:center;justify-content:center;user-select:none;-webkit-user-select:none}'
    +'#csel-annot-btn:hover{border-color:#5b6575}'
    +'#csel-annot-btn.on{background:#5b9dff;color:#fff;border-color:#5b9dff}'
    +'#csel-annot-overlay{position:absolute;left:0;top:0;z-index:900;pointer-events:none;background:transparent}';
  var akSt = document.createElement('style'); akSt.textContent = akBtnCss;
  document.head.appendChild(akSt);

  var akBtn = document.createElement('button');
  akBtn.id = 'csel-annot-btn';
  akBtn.title = 'Dessiner des annotations sur ce document';
  akBtn.textContent = '✎';                 // ✎
  document.body.appendChild(akBtn);

  var akOverlay = document.createElement('canvas');
  akOverlay.id = 'csel-annot-overlay';
  document.body.appendChild(akOverlay);

  var annot = null;                             // AnnotKit api (lazy)

  function docSize(){
    var de = document.documentElement, b = document.body;
    var w = Math.max(de.scrollWidth, b ? b.scrollWidth : 0, de.clientWidth);
    var h = Math.min(20000, Math.max(de.scrollHeight, b ? b.scrollHeight : 0, de.clientHeight));
    return { w: w, h: h };
  }
  function sizeOverlay(){
    var d = docSize();
    akOverlay.width = d.w; akOverlay.height = d.h;      // pixel space = CSS size (1:1)
    akOverlay.style.width = d.w + 'px'; akOverlay.style.height = d.h + 'px';
    if (annot && annot.enabled) annot.redraw();
  }

  function makeHost(){
    return {
      overlay: akOverlay,
      name: function(){ return NAME + '-annot'; },
      exportBase: function(){
        var d = docSize();
        return fetch('/rasterize?path=' + encodeURIComponent(REL) + '&w=' + d.w + '&h=' + d.h)
          .then(function(r){ if (!r.ok) throw new Error('rasterize ' + r.status); return r.blob(); })
          .then(function(blob){ return new Promise(function(res, rej){
            var img = new Image();
            img.onload = function(){ res({ src: img, w: img.naturalWidth, h: img.naturalHeight }); };
            img.onerror = rej;
            img.src = URL.createObjectURL(blob);
          }); });
      }
    };
  }

  function loadKit(){
    return new Promise(function(res, rej){
      if (window.AnnotKit) return res();
      var s = document.createElement('script');
      s.src = '/.fig_thumbs/annot_kit.js';       // absolute: reports live anywhere in the tree
      s.onload = function(){ res(); };
      s.onerror = function(){ rej(new Error('annot_kit load failed')); };
      document.head.appendChild(s);
    });
  }

  akBtn.addEventListener('click', function(){
    loadKit().then(function(){
      if (!annot){
        sizeOverlay();
        annot = window.AnnotKit.create(makeHost());
      }
      var on = annot.toggle();
      akBtn.classList.toggle('on', on);
      if (on){ hideAll(); sizeOverlay(); }         // don't fight the text-selection pill
    }).catch(function(){ akBtn.textContent = '!'; setTimeout(function(){ akBtn.textContent = '✎'; }, 1600); });
  });

  window.addEventListener('resize', function(){ if (annot && annot.enabled) sizeOverlay(); });
})();
