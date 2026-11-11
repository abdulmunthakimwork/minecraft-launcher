// Installation workspace. The official UI and harness dialog primitives own
// the visual furniture; all installation data and commands use the local host.
(function () {
  'use strict';
  if (window.__installationWorkspace) return;
  var current = null, sequence = 0, jobs = Object.create(null);
  var categories = [['general','General'],['mods','Mods'],['resourcepacks','Resource packs'],['shaderpacks','Shaders'],['worlds','Worlds'],['screenshots','Screenshots'],['servers','Servers'],['logs','Logs'],['notes','Notes'],['advanced','Advanced']];
  function style() {
    if (document.getElementById('__iw-style')) return;
    var s = document.createElement('style'); s.id = '__iw-style';
    s.textContent = `
      #__hx-screen[data-installation-id] .__hx-body{padding:0;overflow:hidden;align-items:stretch}
      #__hx-screen[data-installation-id] .__hx-inner{display:flex;width:100%;max-width:1120px;min-height:0}
      #__hx-screen .iw-rail{flex:0 0 176px;padding:20px 12px;overflow:auto;border-right:1px solid rgba(65,65,65,.4)}
      #__hx-screen .iw-category{display:block;width:100%;text-align:left;background:transparent;border:1px solid transparent;color:#fff;padding:10px 12px;font:700 12px/16px Arial;cursor:pointer}
      #__hx-screen .iw-category:hover{background:#3d3938}
      #__hx-screen .iw-category[aria-selected=true]{background:#262423;border-left:3px solid #86d562;padding-left:10px}
      #__hx-screen .iw-category:focus,#__hx-screen .iw-list button:focus{outline:2px solid white;box-shadow:0 0 0 3px #1e6eea;outline-offset:-2px}
      #__hx-screen .iw-pane{flex:1;min-width:0;overflow:auto;padding:32px 32px 20px;font-size:12px;line-height:18px;overflow-wrap:anywhere}
      #__hx-screen .iw-general{max-width:550px;margin:0 auto}
      #__hx-screen .iw-pane h3{font-size:16px;line-height:20px;margin:0 0 20px}
      #__hx-screen .iw-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:20px}
      #__hx-screen .iw-toolbar .__hx-btn+.__hx-btn{margin-left:0}
      #__hx-screen [data-iw-search-reset][hidden]{display:none}
      #__hx-screen .iw-error{margin:0 0 16px;min-height:0;color:#ff6b5b}
      #__hx-screen .iw-status{margin:0 0 20px;color:#a7a7a7;overflow-wrap:anywhere}
      #__hx-screen .iw-list{list-style:none;padding:0;margin:0}
      #__hx-screen .iw-list>li{margin-bottom:4px;min-width:0}
      #__hx-screen .iw-fallback-row{display:flex;align-items:center;gap:16px;padding:16px 12px;background:#262423;min-width:0}
      #__hx-screen .iw-fallback-row:hover{background:#3d3938}
      #__hx-screen .iw-row-copy{flex:1;min-width:0}
      #__hx-screen .iw-row-name{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:700}
      #__hx-screen .iw-row-detail{display:block;color:#a7a7a7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      #__hx-screen .iw-list [data-iw-native-row] img{max-width:48px;max-height:48px}
      #__hx-screen .iw-list [data-iw-native-row] button{max-width:150px}
      #__hx-screen .iw-logo{display:inline-flex;align-items:center;justify-content:center;flex:0 0 48px;width:48px;height:48px;vertical-align:middle}
      #__hx-screen .iw-logo img{display:block;width:100%;height:100%;max-width:100%;max-height:100%;object-fit:contain;image-rendering:auto}
      #__hx-screen .iw-logo img[hidden],#__hx-screen .iw-logo-fallback[hidden]{display:none}
      #__hx-screen .iw-logo-fallback{display:flex;align-items:center;justify-content:center;width:100%;height:100%;box-sizing:border-box;background:#262423;border:1px solid #514d4c;color:#a7a7a7;font:700 16px/20px Arial}
      #__hx-screen .iw-project-header{display:flex;align-items:center;gap:16px;margin-bottom:16px;min-width:0}
      #__hx-screen .iw-project-header h3{margin:0;min-width:0}
      #__hx-screen [data-iw-project-link]{color:#fff;font-size:inherit;font-weight:700;padding:1px 6px 3px;position:relative;white-space:normal}
      #__hx-screen [data-iw-project-link]:hover,#__hx-screen [data-iw-project-link]:focus{background-color:rgba(255,255,255,.2);border-radius:3px;text-decoration:underline;outline:0}
      #__hx-screen [data-iw-project-link]:active{background-color:rgba(255,255,255,.15);border-radius:3px;text-decoration:underline}
      #__hx-screen .iw-review-file{display:flex;align-items:center;gap:16px;padding:12px 0;border-bottom:1px solid rgba(65,65,65,.4)}
      #__hx-screen .iw-review-file .iw-row-copy{overflow-wrap:anywhere}
      #__hx-screen .iw-review-file p{margin:4px 0 0;color:#a7a7a7}
      #__hx-screen .iw-pane h4{font-size:12px;line-height:18px;margin:20px 0 8px}
      #__hx-screen .iw-job-progress{display:block;width:100%;height:14px;accent-color:#3c8527;margin:0 0 12px}
      #__hx-screen .iw-check{display:flex;align-items:center;gap:8px;text-transform:none;margin:0 0 16px}
      #__hx-screen .iw-check input{accent-color:#3c8527;width:18px;height:18px}
      #__hx-screen .iw-pair{display:flex;gap:16px}.iw-pair>div{flex:1;min-width:0}
      #__hx-screen .iw-picker{position:relative;width:280px;max-width:100%;margin-bottom:24px}
      #__hx-screen .iw-trigger{width:100%;padding:7px 10px;height:34px;text-align:left;background:#262423;border:2px solid #86d562;border-radius:3px;color:#fff;font:12px/16px Arial;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      #__hx-screen .iw-trigger:hover{background:#3d3938}#__hx-screen .iw-trigger:focus{border-color:#fff;outline:1px solid #fff}
      #__hx-screen .iw-options{position:absolute;top:100%;left:0;z-index:10;width:274px;max-width:calc(100% - 6px);max-height:340px;overflow:auto;padding:2px;background:#312d2c;border:1px solid #3c8527;list-style:none;margin:0;display:flex;flex-direction:column;gap:4px}
      #__hx-screen .iw-options[hidden]{display:none}#__hx-screen .iw-options button{display:block;width:100%;padding:1px 6px;color:#fff;background:#312d2c;border:1px solid #312d2c;outline:1px solid #312d2c;text-align:left;font:12px/16px Arial;cursor:pointer;transition:background-color 120ms,border-color 120ms}
      #__hx-screen .iw-options button:hover{background:#514d4c}#__hx-screen .iw-options button:focus{background:#514d4c;border-color:#1e6eea;outline-color:#fff;outline-offset:1px}
      #__hx-screen .iw-pane textarea{display:block;width:100%;box-sizing:border-box;min-height:220px;resize:vertical;background:#262423;color:white;border:2px solid #86d562;border-radius:3px;padding:7px 10px;font:12px/18px Arial}
      #__hx-screen .iw-pane textarea:focus{border-color:#fff;outline:1px solid white;background:#3d3938}
      #__hx-screen .iw-logs{white-space:pre-wrap;background:#262423;padding:12px;max-height:330px;overflow:auto;font:12px/18px monospace}
      #__hx-screen .iw-diagnosis{margin:0 0 20px}
      #__hx-screen .iw-diagnosis h4{margin-top:0}
      #__hx-screen .iw-log-finding{background:#262423;border-left:3px solid #514d4c;padding:16px;margin:12px 0}
      #__hx-screen .iw-log-finding p{margin:8px 0}
      #__hx-screen .iw-log-evidence{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/18px monospace;margin:4px 0 12px}
      #__hx-screen[data-hx-magnified=true] .iw-log-evidence{font-size:20px;line-height:24px}
      #__hx-screen .iw-image{max-width:100%;max-height:340px;object-fit:contain;margin-bottom:16px}
      #__hx-screen .iw-icon-trigger{display:flex;align-items:center;padding:7px;background:transparent;color:white;border:0;border-radius:4px;cursor:pointer;margin-bottom:20px}
      #__hx-screen .iw-icon-trigger:hover{background:#3d3938}#__hx-screen .iw-icon-trigger:focus{outline:2px solid white;box-shadow:0 0 0 3px #1e6eea}
      #__hx-screen .iw-icon-trigger img{width:50px;height:50px;margin-right:7px;image-rendering:pixelated}
      #__hx-screen .iw-icon-trigger:disabled{cursor:default}#__hx-screen .iw-icon-trigger:disabled:hover{background:transparent}
      #__hx-screen .iw-icon-menu{display:flex;flex-wrap:wrap;gap:0;background:#262423;border-radius:3px;margin:20px 0;max-height:240px;overflow:auto}
      #__hx-screen .iw-icon-menu[hidden]{display:none}#__hx-screen .iw-icon-menu button{padding:4px;background:transparent;border:0;border-radius:3px;cursor:pointer}
      #__hx-screen .iw-icon-menu button:hover{background:#514d4c}#__hx-screen .iw-icon-menu button:focus{outline:2px solid white;box-shadow:0 0 0 3px #1e6eea;outline-offset:-2px}
      #__hx-screen .iw-icon-menu img{width:50px;height:50px;image-rendering:pixelated;display:block}
      #__hx-screen .iw-footer-note{margin:0 auto 0 0;font-size:12px;color:#a7a7a7;max-width:45%;overflow-wrap:anywhere}
      #__hx-screen .iw-select-row{position:relative;padding-left:40px;box-sizing:border-box}
      #__hx-screen .iw-select-row[data-iw-selected=true]{background:#262423}
      #__hx-screen .iw-pick-target{position:absolute;left:0;top:50%;transform:translateY(-50%);width:40px;min-height:44px;display:flex;align-items:center;justify-content:center;cursor:pointer}
      #__hx-screen .iw-pick-target input{width:18px;height:18px;margin:0;accent-color:#3c8527;cursor:pointer}
      #__hx-screen .iw-pick-target:focus-within{outline:2px solid white;box-shadow:0 0 0 3px #1e6eea;outline-offset:-3px}
      #__hx-screen .iw-content-filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;margin-bottom:16px}
      #__hx-screen .iw-content-filters>div{flex:1;min-width:180px;margin-bottom:0}
      #__hx-screen .iw-content-filters .iw-picker{max-width:280px}
      #__hx-screen .iw-selection-note{color:#a7a7a7;margin:0 0 12px}
      #__hx-screen .iw-basket-row{margin-bottom:16px}
      #__hx-screen .iw-basket-row .iw-toolbar{margin:8px 0 0}
      #__hx-screen[data-hx-magnified=true] .iw-select-row{padding-left:48px}
      #__hx-screen[data-hx-magnified=true] .iw-pick-target{width:48px;min-height:48px}
      #__hx-screen[data-hx-magnified=true] .iw-pick-target input{width:22px;height:22px}
      #__hx-screen .__hx-foot button[hidden]{display:none}
      #__hx-screen .iw-fallback-row .iw-toolbar,#__hx-screen .iw-fallback-row .iw-check{margin-bottom:0}
      #__hx-screen .iw-field-browse{font:12px/16px Arial;align-self:stretch;background:transparent;border:0;color:white;padding:0 10px;cursor:pointer}
      #__hx-screen .iw-field-browse:hover{background:#3d3938}#__hx-screen .iw-field-browse:focus{outline:2px solid white;box-shadow:0 0 0 3px #1e6eea;outline-offset:-2px}
      #__hx-screen[data-hx-magnified=true] .iw-field-browse{font:20px/24px Arial}
      #__hx-screen[data-hx-magnified=true] .iw-category{font-size:20px;line-height:24px;padding:12px 10px}
      #__hx-screen[data-hx-magnified=true] .iw-rail{flex-basis:208px}
      #__hx-screen[data-hx-magnified=true] .iw-pane{font-size:20px;line-height:24px;padding:24px}
      #__hx-screen[data-hx-magnified=true] .iw-pane h3{font-size:24px;line-height:28px}
      #__hx-screen[data-hx-magnified=true] .iw-pane h4{font-size:20px;line-height:24px}
      #__hx-screen[data-hx-magnified=true] .iw-trigger{height:42px;font:20px/24px Arial}
      #__hx-screen[data-hx-magnified=true] .iw-options button{font:20px/24px Arial}
      #__hx-screen[data-hx-magnified=true] .iw-pane textarea{font:20px/24px Arial}
      #__hx-screen[data-hx-magnified=true] .iw-footer-note{font-size:20px;line-height:24px}
      #__hx-screen[data-hx-magnified=true] .iw-logs{font-size:20px;line-height:24px}
      @media(max-width:1050px){#__hx-screen .iw-pane{padding:24px 20px}}
    `;
    document.head.appendChild(s);
  }
  function copy(value) { return JSON.parse(JSON.stringify(value)); }
  function installationName(instance){var profile=instance.profile || {};if(profile.name && String(profile.name).trim())return String(profile.name);var type=profile.type || profile.lastVersionId;if(type==='latest-release' || type==='latest')return 'Latest release';if(type==='latest-snapshot')return 'Latest snapshot';return instance.context && instance.context.minecraft?'Minecraft '+instance.context.minecraft:'Installation';}
  function live(s) { return current === s && s.screen.el.isConnected && !s.closed; }
  function dispose(s) {
    if (!s || s.closed) return;
    s.closed = true; s.requests.forEach(function (c) { if(!c.mutation)c.abort(); }); s.requests.clear();
    clearTimeout(s.poll);clearTimeout(s.logPoll); if (s.observer) s.observer.disconnect();
    if(s.escapeMenu)window.removeEventListener('keydown',s.escapeMenu,true);
    if (current === s) current = null;
  }
  function endpoint(s, route) { return '/__instances/' + encodeURIComponent(s.id) + route; }
  function syncHost(data){var host=window.__host,seed=host && host.appData;if(!seed || typeof host.push!=='function')return;if(data.gameVersions){seed.gameVersions=data.gameVersions;host.push('gameVersions');}if(data.profiles){seed.configurationData=seed.configurationData || {};seed.configurationData.profiles=data.profiles;host.push('configurationData');}}
  async function request(s, route, body) {
    var controller = new AbortController(); controller.mutation=body!==undefined; s.requests.add(controller);
    try {
      var response = await fetch(endpoint(s,route), {method:body === undefined ? 'GET':'POST',headers:body === undefined ? {}:{'Content-Type':'application/json'},body:body === undefined ? undefined:JSON.stringify(body),signal:controller.signal});
      var data = await response.json();
      if (!response.ok || data.error || data.ok === false) { var error = new Error(data.error || 'The launcher could not complete this action.'); error.code = data.code; error.details = data.details; throw error; }
      if(body!==undefined)syncHost(data);
      return data;
    } finally { s.requests.delete(controller); }
  }
  function clear(s) {clearTimeout(s.logPoll);s.logPoll=null;s.pane.textContent='';s.pane.removeAttribute('data-iw-job-id');s.screen.el.querySelectorAll('[data-iw-flow-footer]').forEach(function(n){n.remove();});s.epoch++;s.error=null;s.selectionView=null;}
  function node(s,tag,text,cls,parent) { var n=document.createElement(tag); if(text != null)n.textContent=text; if(cls)n.className=cls;(parent || s.pane).appendChild(n);return n; }
  function button(s,label,fn,parent,primary,attribute) {
    var b=s.screen.button(label,!!primary,fn); (parent || s.pane).appendChild(b);
    if(attribute)b.setAttribute(attribute,''); return b;
  }
  function status(s,text,error) {
    if(!live(s))return;
    var n=s.pane.querySelector('[data-iw-status]');
    if(!n){n=node(s,'p','','iw-status');n.dataset.iwStatus='';n.setAttribute('role','status');}
    n.className=error?'iw-error':'iw-status'; n.setAttribute('role',error?'alert':'status');n.textContent=text || '';
  }
  function fail(s,error,epoch) { if(live(s) && (epoch===undefined || s.epoch===epoch) && error.name!=='AbortError') {status(s,error.message || String(error),true);failureDetails(s,error);} }
  function heading(s,text) { node(s,'h3',text); }
  function toolbar(s) { return node(s,'div',null,'iw-toolbar'); }
  function labelButton(b,text){var leaf=Array.from(b.querySelectorAll('span')).filter(function(n){return !n.children.length;}).pop();(leaf || b).textContent=text;}
  function changed(s) {return s.base && (s.memoryInvalid || JSON.stringify(s.draft)!==JSON.stringify(s.base));}
  function readRecovery(s,error,epoch,retry,back,parent) {
    if(!live(s) || s.epoch!==epoch || error.name==='AbortError')return;
    if(parent){parent.textContent='';node(s,'p',error.message || String(error),'iw-error',parent).setAttribute('role','alert');}
    else fail(s,error,epoch);
    var actions=parent?node(s,'div',null,'iw-toolbar',parent):toolbar(s),pending=false;
    button(s,'Retry',function(){if(pending || !live(s) || s.epoch!==epoch)return;pending=true;actions.querySelectorAll('button').forEach(function(b){b.disabled=true;});retry();},actions,false,'data-iw-read-retry');
    if(back)button(s,'Back',function(){if(!live(s) || s.epoch!==epoch)return;back();},actions,false,'data-iw-read-back');
  }
  function toolDraftKey(s,kind){return kind+'|'+(s.instance && s.instance.ownership && s.instance.ownership.root || '');}
  function toolDraftChanged(draft){return draft && JSON.stringify(draft.value)!==JSON.stringify(draft.base);}
  function toolDraft(s,kind,value,revision){s.toolDrafts=s.toolDrafts || Object.create(null);var key=toolDraftKey(s,kind),draft=s.toolDrafts[key];if(!draft || (!draft.pending && !toolDraftChanged(draft)))draft=s.toolDrafts[key]={base:copy(value),value:copy(value),revision:revision};return draft;}
  function refreshFooter(s) {
    if(!live(s))return;
    var dirty=changed(s),job=jobs[s.id];
    var unsaved=['notes','servers'].filter(function(kind){return s.toolDrafts && toolDraftChanged(s.toolDrafts[toolDraftKey(s,kind)]);});
    labelButton(s.closeButton,dirty || unsaved.length?'Cancel':(s.committed || job?'Close':'Cancel'));
    s.saveButton.hidden=s.category!=='general' && !dirty;
    s.saveButton.disabled=!s.instance || s.busy || !dirty;
    refreshContentControls(s);
    s.footerNote.textContent=[dirty?'General changes are not saved.':'',unsaved.length?'Unsaved '+unsaved.join(' and ')+' changes.':'',job && !['succeeded','complete','failed','cancelled','idle'].includes(job.status)?'The content operation continues when closed.':''].filter(Boolean).join(' ');
  }
  function values(instance) {
    var p=instance.profile || {},c=instance.context || {},r=p.resolution || {};
    return {name:p.name || '',icon:p.icon || 'Grass',minecraft:c.minecraft || p.lastVersionId || '',loader:c.loader || 'vanilla',loaderVersion:c.loaderVersion || '',gameDir:p.gameDir || '',width:r.width || '',height:r.height || '',javaDir:p.javaDir || '',javaArgs:p.javaArgs || ''};
  }
  function draftField(s,parent,key,label,placeholder) {
    var id='iw-'+(key==='gameDir'?'directory':key);parent.insertAdjacentHTML('beforeend',s.ui.field(id,label,placeholder,s.draft[key]));
    var input=parent.querySelector('#'+id);input.addEventListener('input',function(){s.draft[key]=input.value;input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');refreshFooter(s);});return input;
  }
  // Same 280px ToggleList geometry and 4px option rhythm as native module
  // 73627. Keyboard navigation stays within the menu and Escape closes it.
  function picker(s,parent,key,label,choices,onChange,disabled,state) {
    var values=state || s.draft;
    var wrap=node(s,'div',null,'iw-picker',parent),lab=node(s,'label',label,null,wrap);lab.id='iw-'+key+'-label';
    var trigger=node(s,'button',null,'iw-trigger',wrap);trigger.type='button';trigger.id='iw-'+key;trigger.setAttribute('aria-labelledby',lab.id);trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');trigger.disabled=!!disabled;
    var list=node(s,'ul',null,'iw-options scrollbars-default',wrap);list.id='iw-'+key+'-options';list.hidden=true;list.setAttribute('role','listbox');trigger.setAttribute('aria-controls',list.id);
    function close(){list.hidden=true;trigger.setAttribute('aria-expanded','false');}
    function open(){if(trigger.disabled)return;list.hidden=false;trigger.setAttribute('aria-expanded','true');var selected=list.querySelector('[aria-selected=true] button') || list.querySelector('button');if(selected)selected.focus();}
    choices.forEach(function(choice){var value=typeof choice==='string'?choice:choice.value,label=typeof choice==='string'?choice:choice.label;var li=node(s,'li',null,null,list);li.setAttribute('role','option');li.setAttribute('aria-selected',String(values[key]===value));var option=node(s,'button',label,null,li);option.type='button';option.addEventListener('click',function(){values[key]=value;close();trigger.textContent=label;list.querySelectorAll('[role=option]').forEach(function(row){row.setAttribute('aria-selected',String(row===li));});refreshFooter(s);trigger.focus();if(onChange)onChange(value);});});
    var selected=choices.find(function(c){return (typeof c==='string'?c:c.value)===values[key];});trigger.textContent=selected?(typeof selected==='string'?selected:selected.label):values[key] || 'Select '+label.toLowerCase();
    trigger.addEventListener('click',function(){list.hidden?open():close();});
    wrap.addEventListener('keydown',function(e){var options=Array.from(list.querySelectorAll('button')),i=options.indexOf(document.activeElement),keys=['ArrowDown','ArrowUp','Home','End','Escape'];if(e.key==='Enter'||e.key===' '){e.preventDefault();e.stopPropagation();if(i>=0)options[i].click();else list.hidden?open():close();return;}if(!keys.includes(e.key))return;e.preventDefault();e.stopPropagation();if(e.key==='Escape'){close();trigger.focus();return;}if(list.hidden){open();return;}if(e.key==='Home')i=0;else if(e.key==='End')i=options.length-1;else i=(i+(e.key==='ArrowDown'?1:-1)+options.length)%options.length;if(options[i])options[i].focus();});
    s.screen.el.addEventListener('click',function(e){if(!wrap.contains(e.target))close();});return trigger;
  }
  function general(s) {
    clear(s); var form=node(s,'div',null,'iw-general');
    if(!s.instance){status(s,'Loading installation…');return;}
    var last=s.jobPoll && s.jobPoll.last || s.instance.latestJob;
    if(last && last.id && last.status!=='idle' && ['succeeded','complete','failed','cancelled'].includes(last.status)){node(s,'p','Last operation: '+(last.message || last.status),'__hx-note',form);var recent=node(s,'div',null,'iw-toolbar',form);button(s,'View last operation',function(){progress(s,last.id);},recent,false,'data-iw-last-operation');}
    var dynamic=['latest-release','latest-snapshot'].includes(s.instance.profile.type) || ['latest-release','latest-snapshot'].includes(s.draft.minecraft);
    draftField(s,form,'name','Name').disabled=dynamic;
    iconPicker(s,form,dynamic);
    picker(s,form,'minecraft','Minecraft version',(s.instance.versions || []).map(function(v){return {value:v.id,label:(v.type || 'release')+' '+v.id};}),function(){s.draft.loaderVersion='';general(s);},dynamic);
    picker(s,form,'loader','Loader',(s.instance.loaderChoices || ['vanilla','fabric','quilt','forge','neoforge']).map(function(v){return {value:v,label:{vanilla:'Vanilla',fabric:'Fabric',quilt:'Quilt',forge:'Forge',neoforge:'NeoForge'}[v] || v};}),function(){s.draft.loaderVersion='';general(s);},dynamic);
    if(s.draft.loader!=='vanilla') {
      var loaderGroup=node(s,'div',null,null,form),epoch=s.epoch;
      var loaderId={fabric:'fabric-loader',quilt:'quilt-loader'}[s.draft.loader] || s.draft.loader;
      var loaderPending=false;
      function loadVersions(){if(loaderPending || !live(s) || s.epoch!==epoch)return;loaderPending=true;loaderGroup.textContent='Loading compatible loader versions…';var controller=new AbortController();s.requests.add(controller);
        fetch('/__loaders/versions?minecraft='+encodeURIComponent(s.draft.minecraft)+'&loader='+encodeURIComponent(loaderId),{signal:controller.signal}).then(async function(r){var data=await r.json();if(!r.ok || data.error)throw new Error(data.error || 'Compatible loader versions could not be loaded.');return data;}).then(function(data){if(!live(s)||s.epoch!==epoch)return;loaderGroup.textContent='';picker(s,loaderGroup,'loaderVersion','Loader version',(data.versions || []).map(function(v){return typeof v==='string'?v:{value:v.version || v.id,label:v.version || v.id};}),null,dynamic);}).catch(function(e){readRecovery(s,e,epoch,loadVersions,null,loaderGroup);}).finally(function(){loaderPending=false;s.requests.delete(controller);});
      }loadVersions();
    }
    var directory=draftField(s,form,'gameDir','Game directory','Use the default directory');browseField(s,directory,'gameDir','open_folder','data-iw-browse-directory');
    var pair=node(s,'div',null,'iw-pair',form);draftField(s,node(s,'div',null,null,pair),'width','Resolution width','854');draftField(s,node(s,'div',null,null,pair),'height','Resolution height','480');
    var java=draftField(s,form,'javaDir','Java executable','Use bundled Java');browseField(s,java,'javaDir','open_file','data-iw-browse-java');draftField(s,form,'javaArgs','JVM arguments','Use default arguments');
    memoryField(s,form);
    if(dynamic)node(s,'p','This latest-version installation follows launcher updates. Make a pinned independent copy before adding managed content.','__hx-note',form);
    var note=node(s,'p','','iw-error',form);note.id='iw-general-error';note.setAttribute('role','alert');
    refreshFooter(s);
  }
  async function save(s,after) {
    if(!live(s) || s.busy || !s.instance)return;
    var error='',key='name';
    if(s.memoryInvalid){error='Maximum memory must be a whole number between 256 and 65536 MiB, or blank to inherit defaults.';key='memory';}
    else if(!s.draft.name.trim() && !['latest-release','latest-snapshot'].includes(s.instance.profile.type))error='Enter an installation name.';
    else if((s.draft.width && !/^\d+$/.test(String(s.draft.width))) || (s.draft.height && !/^\d+$/.test(String(s.draft.height)))){error='Resolution must contain positive whole numbers.';key=!/^\d+$/.test(String(s.draft.width))?'width':'height';}
    else if((s.draft.width || s.draft.height) && (!s.draft.width || !s.draft.height || +s.draft.width<300 || +s.draft.width>9999 || +s.draft.height<260 || +s.draft.height>9999)){error='Enter a width from 300 to 9999 and a height from 260 to 9999.';key=!s.draft.width || +s.draft.width<300 || +s.draft.width>9999?'width':'height';}
    else if(s.draft.loader!=='vanilla'&&!s.draft.loaderVersion){error='Choose a compatible loader version.';key='loaderVersion';}
    if(error){if(s.category!=='general')select(s,'general');var field=s.pane.querySelector('#iw-'+key),notice=s.pane.querySelector('#iw-general-error');notice.textContent=error;if(field){field.setAttribute('aria-invalid','true');field.setAttribute('aria-describedby',notice.id);field.focus();}return;}
    s.busy=true;refreshFooter(s);
    var patch={name:s.draft.name.trim(),icon:s.draft.icon,minecraft:s.draft.minecraft,loader:s.draft.loader,loaderVersion:s.draft.loaderVersion,gameDir:s.draft.gameDir,javaDir:s.draft.javaDir,javaArgs:s.draft.javaArgs,resolution:s.draft.width || s.draft.height ? {width:Number(s.draft.width) || 854,height:Number(s.draft.height) || 480}:null};
    Object.keys(patch).forEach(function(key){if(key==='resolution'){if(String(s.draft.width)===String(s.base.width) && String(s.draft.height)===String(s.base.height))delete patch[key];}else if(String(s.draft[key])===String(s.base[key]))delete patch[key];});
    var submitted=JSON.stringify(s.draft);
    try {var data=await request(s,'/general',{patch:patch});if(!live(s))return;var newer=JSON.stringify(s.draft)!==submitted || s.memoryInvalid;s.instance=data;s.base=values(data);s.inventory=null;s.inventoryScan++;s.inventoryError=null;s.plan=null;s.activationDraft=null;if(newer){status(s,'Saved. Your newer General changes are still unsaved.');return;}s.draft=copy(s.base);s.memoryDraft=null;if(after){s.busy=false;after();}else requestClose(s);}
    catch(e){fail(s,e);}finally{s.busy=false;refreshFooter(s);}
  }
  function close(s) { if(!live(s))return;dispose(s);s.screen.close();if(s.opener && s.opener.isConnected)s.opener.focus(); }
  function unsavedCategories(s){var names=[];if(changed(s))names.push('General');['notes','servers'].forEach(function(kind){if(Object.keys(s.toolDrafts || {}).some(function(key){return key.indexOf(kind+'|')===0 && toolDraftChanged(s.toolDrafts[key]);}))names.push(kind==='notes'?'Notes':'Servers');});return names;}
  function requestClose(s){
    if(!live(s))return;var unsaved=unsavedCategories(s);if(!unsaved.length){close(s);return;}
    if(s.closeNotice && s.closeNotice.isConnected){s.closeNotice.querySelector('[data-iw-keep-editing]').focus();return;}
    var focused=document.activeElement,notice=node(s,'div',null,'__hx-row');s.pane.insertBefore(notice,s.pane.firstChild);s.closeNotice=notice;notice.setAttribute('role','alert');notice.setAttribute('data-iw-unsaved-close','');node(s,'h3','Unsaved installation changes',null,notice);node(s,'p',unsaved.join(', ')+' changes are not saved. Keep editing or discard these drafts to close. Completed file operations remain saved.','__hx-note',notice);
    var actions=node(s,'div',null,'iw-toolbar',notice);var keep=button(s,'Keep editing',function(){notice.remove();if(focused && focused.isConnected)focused.focus();},actions,true,'data-iw-keep-editing');button(s,'Discard unsaved changes',function(){close(s);},actions,false,'data-iw-discard-close');keep.focus();notice.scrollIntoView({block:'nearest'});
  }
  function browseField(s,input,key,mode,attribute) {
    if(!window.MCLauncher || typeof window.MCLauncher.openFileDialog!=='function')return;
    var wrapper=node(s,'div',null,null,input.parentElement);wrapper.style.cssText='display:flex;align-items:center;background:#262423;border-radius:3px';wrapper.appendChild(input);input.style.cssText='flex:1;min-width:0';var browse=node(s,'button','Browse','iw-field-browse',wrapper);browse.type='button';browse.setAttribute(attribute,'');browse.addEventListener('click',async function(){var epoch=s.epoch,prior=s.draft[key];try{var path=await window.MCLauncher.openFileDialog({mode:mode,path:prior});if(!live(s)||s.epoch!==epoch || s.draft[key]!==prior || !path)return;s.draft[key]=String(path);input.value=s.draft[key];refreshFooter(s);}catch(e){fail(s,e,epoch);}});
  }
  function iconPicker(s,parent,disabled) {
    node(s,'label','Icon',null,parent).htmlFor='iw-icon';var trigger=node(s,'button',null,'iw-icon-trigger',parent);trigger.type='button';trigger.id='iw-icon';trigger.disabled=disabled;trigger.setAttribute('aria-label','Installation icon');trigger.setAttribute('aria-expanded','false');
    var image=node(s,'img',null,null,trigger);image.alt='Installation icon';function url(id){return /^data:image\//.test(id)?id:'/launcher/assets/icons/javaIcons/'+encodeURIComponent(id || 'Furnace')+'.png';}image.src=url(s.draft.icon);node(s,'span','▾',null,trigger).setAttribute('aria-hidden','true');
    var menu=node(s,'div',null,'iw-icon-menu scrollbars-default',parent);menu.hidden=true;menu.id='iw-icon-menu';menu.setAttribute('role','group');menu.setAttribute('aria-label','Choose installation icon');trigger.setAttribute('aria-controls',menu.id);
    // Native module 17193's artwork IDs and 50px pixel previews.
    var names='Bedrock Bookshelf Brick Cake Carved_Pumpkin Chest Clay Coal_Block Coal_Ore Cobblestone Crafting_Table Creeper_Head Diamond_Block Diamond_Ore Dirt Dirt_Podzol Dirt_Snow Emerald_Block Emerald_Ore Enchanting_Table End_Stone Farmland Furnace Furnace_On Glass Glazed_Terracotta_Light_Blue Glazed_Terracotta_Orange Glazed_Terracotta_White Glowstone Gold_Block Gold_Ore Grass Gravel Hardened_Clay Ice_Packed Iron_Block Iron_Ore Lapis_Ore Leaves_Birch Leaves_Jungle Leaves_Oak Leaves_Spruce Lectern_Book Log_Acacia Log_Birch Log_DarkOak Log_Jungle Log_Oak Log_Spruce Mycelium Nether_Brick Netherrack Obsidian Planks_Acacia Planks_Birch Planks_DarkOak Planks_Jungle Planks_Oak Planks_Spruce Quartz_Ore Red_Sand Red_Sandstone Redstone_Block Redstone_Ore Sand Sandstone Skeleton_Skull Snow Soul_Sand Stone Stone_Andesite Stone_Diorite Stone_Granite TNT Water Wool'.split(' ');
    names.forEach(function(name){var b=node(s,'button',null,null,menu);b.type='button';b.dataset.icon=name;b.setAttribute('aria-label',name.replace(/_/g,' '));var img=node(s,'img',null,null,b);img.src=url(name);img.alt=name.replace(/_/g,' ');b.addEventListener('click',function(){s.draft.icon=name;image.src=url(name);menu.hidden=true;trigger.setAttribute('aria-expanded','false');trigger.focus();refreshFooter(s);});});
    trigger.addEventListener('click',function(){menu.hidden=!menu.hidden;trigger.setAttribute('aria-expanded',String(!menu.hidden));if(!menu.hidden)(menu.querySelector('[data-icon="'+CSS.escape(s.draft.icon)+'"]') || menu.querySelector('button')).focus();});
    menu.addEventListener('keydown',function(e){if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;e.preventDefault();var buttons=Array.from(menu.querySelectorAll('button')),i=buttons.indexOf(document.activeElement),columns=Math.max(1,Math.floor(menu.clientWidth/58));if(e.key==='Home')i=0;else if(e.key==='End')i=buttons.length-1;else i=Math.max(0,Math.min(buttons.length-1,i+({ArrowLeft:-1,ArrowRight:1,ArrowUp:-columns,ArrowDown:columns}[e.key])));buttons[i].focus();});
  }
  function memoryField(s,parent) {
    parent.insertAdjacentHTML('beforeend',s.ui.field('iw-memory','Maximum memory (MiB)','Inherit default memory',''));var input=parent.querySelector('#iw-memory'),argsField=parent.querySelector('#iw-javaArgs'),note=node(s,'p','','__hx-note',parent);note.dataset.iwMemoryNote='';
    function sync(){var tokens=String(s.draft.javaArgs).match(/(?:^|\s)-Xmx\S*/ig) || [],token=tokens[tokens.length-1],match=token && token.trim().match(/^-Xmx(\d+)([gmk])$/i);input.value=s.memoryInvalid && s.memoryDraft!=null?s.memoryDraft:match?String(Math.round(+match[1]*({g:1024,m:1,k:1/1024}[match[2].toLowerCase()]))):'';if(s.memoryInvalid)input.setAttribute('aria-invalid','true');else input.removeAttribute('aria-invalid');note.textContent=s.memoryInvalid?'Correct maximum memory or edit the JVM arguments to resolve this draft.':token?match?'This installation overrides maximum memory. Other JVM arguments are retained.':'JVM arguments define memory directly. Edit those arguments or enter a maximum memory value.':'Maximum memory inherits the launcher’s default JVM arguments.';}
    argsField.addEventListener('input',function(){s.memoryInvalid=false;s.memoryDraft=null;sync();refreshFooter(s);});
    input.addEventListener('input',function(){var value=input.value.trim();s.memoryDraft=input.value;s.memoryInvalid=!!value && (!/^\d+$/.test(value) || +value<256 || +value>65536);if(s.memoryInvalid){sync();refreshFooter(s);return;}var args=String(s.draft.javaArgs),inserted=false;s.draft.javaArgs=args.replace(/(^|\s)-Xmx\S*/ig,function(_all,space){if(value && !inserted){inserted=true;return space+'-Xmx'+value+'M';}return '';}).trim();if(value&&!inserted)s.draft.javaArgs=('-Xmx'+value+'M '+s.draft.javaArgs).trim();s.memoryDraft=null;argsField.value=s.draft.javaArgs;sync();refreshFooter(s);});sync();
  }
  function gateDraft(s,continuation) {
    if(!changed(s))return false;
    clear(s);heading(s,'Unsaved General changes');node(s,'p','Save or discard the General draft before using this installation’s saved directory, version and loader.','__hx-note');
    var actions=toolbar(s);button(s,'Save changes',function(){save(s,continuation);},actions,true);button(s,'Discard changes',function(){s.draft=copy(s.base);s.memoryInvalid=false;s.memoryDraft=null;refreshFooter(s);continuation();},actions);button(s,'Back to General',function(){select(s,'general');},actions);return true;
  }
  function mutationReason(s) {
    if(s.instance.running)return 'The game is running. Stop it before changing installation files.';
    if(!s.instance.ownership || !s.instance.ownership.private)return s.instance.ownership && s.instance.ownership.reason || 'This directory is shared or uses the launcher default. Make an independent copy before changing files.';
    return '';
  }
  function contentMutationReason(s) {
    var job=jobs[s.id],running=job && !['succeeded','complete','failed','cancelled','idle'].includes(job.status);
    return mutationReason(s) || (running?'An installation operation is running. Wait for it to finish before changing files.':'') || (!s.inventory || !s.inventory.revision?(s.inventoryError?s.inventoryError.message+' ':'')+'Refresh installed files before changing content.':'');
  }
  function refreshContentControls(s) {
    if(!live(s) || !s.instance)return;var reason=contentMutationReason(s);
    s.pane.querySelectorAll('[data-iw-import],[data-iw-native-import],[data-iw-plan]').forEach(function(b){b.disabled=s.busy || !!reason;});
    s.pane.querySelectorAll('[data-iw-content-reason]').forEach(function(n){if(reason)n.textContent=reason;else n.remove();});
    s.screen.el.querySelectorAll('[data-iw-locked-control]').forEach(function(b){b.disabled=s.busy || !!reason || b.dataset.iwUnavailable==='true';});
    s.pane.querySelectorAll('[data-iw-select-id],[data-iw-select-project],[data-iw-select-visible],[data-iw-clear-selection],[data-iw-add-selection],[data-iw-edit-pick],[data-iw-remove-pick],[data-iw-retry-pick],[data-iw-basket-optional]').forEach(function(b){b.disabled=s.busy || b.dataset.iwUnavailable==='true';});
    var selected=s.screen.el.querySelector('[data-iw-review-selected]');if(selected){selected.disabled=s.busy || selected.dataset.iwUnavailable==='true';selected.hidden=selected.dataset.iwUnavailable==='true' || changed(s);}
  }
  function inventoryContext(s) {
    var p=s.instance.profile || {},c=s.instance.context || {},o=s.instance.ownership || {};
    return JSON.stringify([s.id,p.gameDir || '',p.lastVersionId || '',c.minecraft || '',c.loader || '',c.loaderVersion || '',o.root || '']);
  }
  function refreshInstanceState(s,data) {
    if(typeof data.gameRunning==='boolean')s.instance.running=data.gameRunning;
    if(data.ownership && typeof data.ownership.private==='boolean')s.instance.ownership=data.ownership;
  }
  function ownership(s) {
    var reason=mutationReason(s);
    if(s.instance.ownership && !s.instance.ownership.private){node(s,'p',s.instance.ownership.reason || 'This installation does not own a private directory.','__hx-note');button(s,'Make independent copy',function(){copyInstallation(s);},toolbar(s),false,'data-iw-copy');}
    if(s.instance.running)node(s,'p','The game is running. Stop it before changing installation files.','__hx-note');return reason;
  }
  async function copyInstallation(s) {
    clear(s);heading(s,'Make independent copy');node(s,'p','Create a separate installation and directory. The original installation stays in place.','__hx-note');s.pane.insertAdjacentHTML('beforeend',s.ui.field('iw-copy-name','Name','',installationName(s.instance)+' copy'));
    var create=button(s,'Create copy',async function(){if(s.busy)return;var name=s.pane.querySelector('#iw-copy-name').value.trim();if(!name){status(s,'Enter a name for the copy.',true);return;}var epoch=s.epoch;s.busy=true;create.disabled=true;refreshFooter(s);try {var data=await request(s,'/copy',{name:name});s.committed=true;if(live(s)&&s.epoch===epoch && !unsavedCategories(s).length)open(data.instanceId,s.opener);else if(live(s))status(s,'Independent copy created: '+(data.profile && data.profile.name || name)+'. It is available in Installations. Your unsaved edits remain in this workspace.');}catch(e){fail(s,e,epoch);}finally{s.busy=false;create.disabled=false;refreshFooter(s);}},toolbar(s),true);
  }
  function rowLines(li) {
    var explicit=li.querySelectorAll('[data-row-title],[data-row-detail]');if(explicit.length)return Array.from(explicit);
    var image=li.querySelector('img'),area=image && image.parentElement;while(area && !area.querySelector('button'))area=area.parentElement;
    return Array.from(li.querySelectorAll('div,span,p')).filter(function(n){return !n.children.length && n.textContent.trim() && !n.closest('button') && !n.querySelector('img');});
  }
  function logoSource(item) {
    if(item && typeof item.iconData==='string' && item.iconData.length<=3*1024*1024 && /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/]+=*$/i.test(item.iconData))return item.iconData;
    var raw=item && (item.iconUrl || item.icon_url);if(typeof raw!=='string' || raw.length>2048)return '';
    try{var url=new URL(raw);if(url.protocol==='https:' && url.hostname==='cdn.modrinth.com' && !url.username && !url.password && !url.port && !url.hash)return '/__content_icon?url='+encodeURIComponent(raw);}catch(e){}return '';
  }
  function logo(s,item,name,parent,existing) {
    var wrap=node(s,'span',null,'iw-logo',parent);wrap.dataset.iwLogo='';wrap.setAttribute('aria-hidden','true');
    if(existing){existing.replaceWith(wrap);}
    var fallback=node(s,'span',String(name || '?').trim().slice(0,2).toUpperCase() || '?','iw-logo-fallback',wrap);fallback.dataset.iwLogoFallback='';
    var src=logoSource(item);if(src){var image=node(s,'img',null,null,wrap);image.alt='';image.hidden=true;image.addEventListener('load',function(){image.hidden=false;fallback.hidden=true;});image.addEventListener('error',function(){image.hidden=true;fallback.hidden=false;});image.src=src;}
    return wrap;
  }
  function projectHeading(s,item,name) {var header=node(s,'div',null,'iw-project-header');header.dataset.iwProjectHeader='';logo(s,item,name,header);node(s,'h3',name,null,header);}
  function targetContext(s) {var c=s.instance.context || {},loader={vanilla:'Vanilla',fabric:'Fabric',quilt:'Quilt',forge:'Forge',neoforge:'NeoForge'}[c.loader] || c.loader || 'Unknown loader';node(s,'p',installationName(s.instance)+' · Minecraft '+(c.minecraft || 'unknown')+' · '+loader+(c.loaderVersion?' '+c.loaderVersion:''),'__hx-note').dataset.iwContentContext='';}
  function fileSize(size) {if(!Number.isFinite(size) || size<0)return 'Size unavailable';if(size>=1048576)return (size/1048576).toFixed(1).replace(/\.0$/,'')+' MiB';if(size>=1024)return (size/1024).toFixed(1).replace(/\.0$/,'')+' KiB';return size+' bytes';}
  function row(s,list,id,name,detail,label,action,project,artwork) {
    var template=document.querySelector('[data-hx-installation-template] li') || Array.from(document.querySelectorAll('main ul li')).find(function(n){return n.querySelector('button[id="MoreOptionsDropdown"]');});
    var li=template ? template.cloneNode(true):document.createElement('li'),b;
    if(template){li.removeAttribute('id');li.removeAttribute('data-configuration-id');li.querySelectorAll('[id]').forEach(function(n){n.removeAttribute('id');});li.dataset.iwNativeRow='';b=li.querySelector('button');li.querySelectorAll('button').forEach(function(n){if(n!==b)n.remove();});li.querySelectorAll('[data-testid="player-safety-icon"]').forEach(function(n){n.remove();});var lines=rowLines(li);if(lines[0])lines[0].textContent=name;if(lines[1])lines[1].textContent=detail;labelButton(b,label);b.disabled=false;b.removeAttribute('aria-disabled');b.removeAttribute('aria-labelledby');b.setAttribute('aria-label',label+' '+name);b.addEventListener('click',function(e){e.preventDefault();action();});}
    else {li.className='iw-fallback-row';var c=node(s,'div',null,'iw-row-copy',li);node(s,'span',name,'iw-row-name',c);node(s,'span',detail,'iw-row-detail',c);b=button(s,label,action,li);}
    if(artwork!==undefined){var existing=li.querySelector('img'),art=logo(s,artwork,name,li,existing);if(!existing)li.insertBefore(art,li.firstChild);}
    li.setAttribute(project?'data-project-id':'data-content-id',id);li.title=name+' — '+detail;list.appendChild(li);return li;
  }
  function selectionState(s) {
    var key=inventoryContext(s);
    if(!s.selections || s.selections.key!==key)s.selections={key:key,installed:Object.create(null),catalog:Object.create(null)};
    return s.selections;
  }
  function installedPicks(s) {
    var state=selectionState(s),kind=s.category;
    if(!state.installed[kind])state.installed[kind]={ids:Object.create(null),query:'',filter:'all',anchor:null};
    var picks=state.installed[kind],available=new Set((s.inventory && s.inventory.items || []).filter(function(i){return i.kind===kind;}).map(function(i){return i.id;}));
    if(s.inventory)Object.keys(picks.ids).forEach(function(id){if(!available.has(id))delete picks.ids[id];});
    return picks;
  }
  function selectedInstalled(s) {var picks=installedPicks(s);return (s.inventory && s.inventory.items || []).filter(function(i){return i.kind===s.category && picks.ids[i.id];});}
  function catalogPicks(s) {
    var state=selectionState(s),kind=s.category;
    if(!state.catalog[kind])state.catalog[kind]={entries:Object.create(null),optional:false};
    return state.catalog[kind];
  }
  function selectionFooter(s,label,action,count) {
    var b=button(s,label,action,s.screen.el.querySelector('.__hx-foot'),true,'data-iw-review-selected');b.dataset.iwFlowFooter='';b.dataset.iwUnavailable=String(!count);b.hidden=!count || changed(s);b.disabled=s.busy || !count;return b;
  }
  function pickCheckbox(s,li,id,name,picked,change,project) {
    li.classList.add('iw-select-row');li.dataset.iwSelected=String(!!picked);
    var label=node(s,'label',null,'iw-pick-target',li),input=node(s,'input',null,null,label);input.type='checkbox';input.checked=!!picked;
    input.setAttribute(project?'data-iw-select-project':'data-iw-select-id',id);input.setAttribute('aria-label','Select '+name);input.disabled=s.busy;
    input.addEventListener('click',function(e){e.stopPropagation();if(s.busy)return;change(input.checked,e.shiftKey);});return input;
  }
  function pickCount(s,text) {var n=node(s,'p',text,'iw-selection-note');n.dataset.iwPickCount='';n.setAttribute('role','status');return n;}
  function installedPickView(s,items,list) {
    var picks=installedPicks(s),filters=node(s,'div',null,'iw-content-filters'),searchWrap=node(s,'div',null,null,filters);
    searchWrap.insertAdjacentHTML('beforeend',s.ui.field('iw-installed-search','Search installed files','Name or filename',picks.query));var search=searchWrap.querySelector('input');
    var filterWrap=node(s,'div',null,null,filters);picker(s,filterWrap,'installed-filter','Show',[{value:'all',label:'All'},{value:'enabled',label:'Enabled'},{value:'disabled',label:'Disabled'}],function(value){picks.filter=value;picks.anchor=null;draw();},false,{'installed-filter':picks.filter});
    var actions=toolbar(s),visibleButton=button(s,'Select visible',function(){var all=visible.length && visible.every(function(i){return picks.ids[i.id];});visible.forEach(function(i){if(all)delete picks.ids[i.id];else picks.ids[i.id]=true;});picks.anchor=null;reflect();},actions,false,'data-iw-select-visible');
    var clearButton=button(s,'Clear selection',function(){picks.ids=Object.create(null);picks.anchor=null;reflect();},actions,false,'data-iw-clear-selection');
    var count=pickCount(s,''),footer=selectionFooter(s,'Review selected',function(){manageSelected(s);},0),visible=[];
    list.setAttribute('tabindex','0');s.selectionView='installed';
    function reflect(){var total=Object.keys(picks.ids).length,hidden=total-visible.filter(function(i){return picks.ids[i.id];}).length;count.textContent=total+' selected'+(hidden?' · '+hidden+' outside this view':'')+' · '+visible.length+' of '+items.length+' shown';labelButton(visibleButton,visible.length && visible.every(function(i){return picks.ids[i.id];})?'Clear visible':'Select visible');visibleButton.dataset.iwUnavailable=String(!visible.length);visibleButton.disabled=s.busy || !visible.length;clearButton.dataset.iwUnavailable=String(!total);clearButton.disabled=s.busy || !total;labelButton(footer,'Review selected ('+total+')');footer.dataset.iwUnavailable=String(!total);footer.hidden=!total || changed(s);footer.disabled=s.busy || !total;list.querySelectorAll('[data-iw-select-id]').forEach(function(input){input.checked=!!picks.ids[input.dataset.iwSelectId];input.closest('li').dataset.iwSelected=String(input.checked);});}
    function draw(){var query=picks.query.trim().toLowerCase();visible=items.filter(function(i){return (!query || [i.name,i.filename,i.version].join(' ').toLowerCase().includes(query)) && (picks.filter==='all' || !!i.enabled===(picks.filter==='enabled'));});list.textContent='';
      visible.forEach(function(i){var li=row(s,list,i.id,i.name || i.filename,[i.version || 'Unknown version',i.enabled?'Enabled':'Disabled',i.provider || 'Local file'].join(' · '),'Details',function(){detailInstalled(s,i);},false,i);pickCheckbox(s,li,i.id,i.name || i.filename,picks.ids[i.id],function(checked,shift){var from=visible.findIndex(function(item){return item.id===picks.anchor;}),to=visible.indexOf(i);var range=shift && from>=0?visible.slice(Math.min(from,to),Math.max(from,to)+1):[i];range.forEach(function(item){if(checked)picks.ids[item.id]=true;else delete picks.ids[item.id];});picks.anchor=i.id;reflect();},false);});
      if(!visible.length)node(s,'p',items.length?'No installed files match this view. Your other selections are kept.':'No files are installed. Browse compatible content or import local files.','__hx-note',list);reflect();}
    search.addEventListener('input',function(){picks.query=search.value;picks.anchor=null;draw();});
    list.addEventListener('keydown',function(e){if((e.ctrlKey || e.metaKey) && e.key.toLowerCase()==='a'){e.preventDefault();e.stopPropagation();visible.forEach(function(i){picks.ids[i.id]=true;});reflect();}});
    s.pane.appendChild(list);draw();
  }
  function manageSelected(s,focusIndex,scroll) {
    var items=selectedInstalled(s);clear(s);s.selectionView='manage';heading(s,'Selected '+categories.find(function(c){return c[0]===s.category;})[1].toLowerCase());targetContext(s);pickCount(s,items.length+' selected');
    var a=toolbar(s),back=button(s,'Back',function(){installed(s);},a);button(s,'Clear selection',function(){installedPicks(s).ids=Object.create(null);installed(s);},a,false,'data-iw-clear-selection');
    var actions=toolbar(s);[['enable','Enable'],['disable','Disable'],['update','Check updates'],['remove','Remove']].forEach(function(pair){var usable=items.length && (pair[0]==='update'?items.some(function(i){return i.projectId;}):pair[0]==='enable'?items.some(function(i){return !i.enabled;}):pair[0]==='disable'?items.some(function(i){return i.enabled;}):true);var b=button(s,pair[1],function(){pair[0]==='update'?updates(s,items.filter(function(i){return i.projectId;}),function(){manageSelected(s);}):previewSelected(s,pair[0]);},actions);b.dataset.iwBulkAction=pair[0];b.dataset.iwLockedControl='';b.dataset.iwUnavailable=String(!usable);});
    if(s.category==='mods'){var enabled=items.filter(function(i){return i.enabled;}),repair=button(s,'Repair dependencies',function(){repairDependencies(s,enabled.map(function(i){return i.id;}),function(){manageSelected(s);});},actions,false,'data-iw-repair-selected');repair.dataset.iwLockedControl='';repair.dataset.iwUnavailable=String(!enabled.length);node(s,'p',enabled.length+' enabled '+(enabled.length===1?'selected mod':'selected mods')+' will be checked. Disabled selections stay disabled unless the review lists them as required.'+(!enabled.length?' Select an enabled mod to check its dependencies.':''),'__hx-note');}
    if(items.some(function(i){return !i.projectId;}))node(s,'p','Updates can be checked for files linked to a catalog project. Local files stay in place.','__hx-note');
    if(s.category!=='mods')node(s,'p','These actions manage installed files. Choose active packs or shaders separately in Minecraft settings.','__hx-note');
    var list=node(s,'ul',null,'iw-list');items.forEach(function(i,index){row(s,list,i.id,i.name || i.filename,(i.version || 'Unknown version')+' · '+(i.enabled?'Enabled':'Disabled'),'Unselect',function(){var scroll=s.pane.scrollTop;delete installedPicks(s).ids[i.id];manageSelected(s,index,scroll);},false,i);});refreshFooter(s);var target=focusIndex===undefined?back:list.children[Math.min(focusIndex,items.length-1)]?.querySelector('button') || back;target.focus({preventScroll:true});if(scroll!==undefined)s.pane.scrollTop=scroll;
  }
  async function previewSelected(s,action,chosen,back) {
    if(s.busy || contentMutationReason(s))return;var items=chosen || selectedInstalled(s);if(!items.length)return;back=back || function(){manageSelected(s);};
    var kind=s.category,body={action:action==='remove'?'remove':'toggle',kind:kind,ids:items.map(function(i){return i.id;}),revision:s.inventory.revision};if(action!=='remove')body.enabled=action==='enable';
    clear(s);heading(s,'Review selected files');targetContext(s);status(s,'Checking dependencies and affected files…');s.busy=true;refreshFooter(s);var epoch=s.epoch;
    try{var preview=await request(s,'/preview-change',body);if(!live(s)||s.epoch!==epoch)return;clear(s);heading(s,(action==='enable'?'Enable':action==='disable'?'Disable':'Remove')+' selected files');targetContext(s);
      [['selected','Selected by you'],['additional',action==='enable'?'Required files to enable':'Dependent files also affected'],['unchanged','Already in the requested state']].forEach(function(pair){if(!(preview[pair[0]] || []).length)return;node(s,'h4',pair[1]);var list=node(s,'ul',null,'iw-list');preview[pair[0]].forEach(function(item){node(s,'li',reviewName(s,{},item)+(item.kind && item.kind!==kind?' · '+(categories.find(function(c){return c[0]===item.kind;}) || [item.kind,item.kind])[1]:''),null,list);});});
      node(s,'p','Apply these changes to the files together. Recoverable transaction history is kept.','__hx-note');reviewMessages(s,{},preview.conflicts,'Conflicts',true);reviewMessages(s,{},preview.warnings,'Warnings',false);
      var a=toolbar(s);button(s,'Back',back,a);var changedItems=preview.changed || (preview.affected || []).filter(function(i){return action==='remove' || !!i.enabled!==body.enabled;});
      var apply=button(s,changedItems.length?(action==='remove'?'Remove files':'Apply changes'):'No changes needed',function(){mutate(s,action==='remove'?'/remove':'/toggle',preview.applyBody);},s.screen.el.querySelector('.__hx-foot'),true,'data-iw-bulk-apply');apply.dataset.iwFlowFooter='';apply.dataset.iwLockedControl='';apply.dataset.iwUnavailable=String(!changedItems.length || !preview.applyBody || !!(preview.conflicts || []).length);
    }catch(e){readRecovery(s,e,epoch,function(){previewSelected(s,action,items,back);},back);}finally{s.busy=false;refreshFooter(s);}
  }
  function catalogSelectionCount(s,store) {
    var entries=Object.values(store.entries),pending=entries.filter(function(e){return e.loading;}).length,errors=entries.filter(function(e){return e.error;}).length;
    return entries.length+' selected'+(pending?' · '+pending+' checking versions':'')+(errors?' · '+errors+' need attention':'');
  }
  function syncCatalogSelection(s,store,kind) {
    if(!live(s) || s.category!==kind)return;
    if(s.selectionView==='basket'){paintBasket(s,store);return;}
    if(!['catalog','project'].includes(s.selectionView))return;
    var n=s.pane.querySelector('[data-iw-pick-count]');if(n)n.textContent=catalogSelectionCount(s,store);
    s.pane.querySelectorAll('[data-iw-select-project]').forEach(function(input){input.checked=!!store.entries[input.dataset.iwSelectProject];input.closest('li').dataset.iwSelected=String(input.checked);});
    var count=Object.keys(store.entries).length,footer=s.screen.el.querySelector('[data-iw-review-selected]');if(footer){labelButton(footer,'Review selected ('+count+')');footer.dataset.iwUnavailable=String(!count);footer.hidden=!count || changed(s);footer.disabled=s.busy || !count;}
  }
  function acquirePick(s,hit,version,force) {
    if(s.busy)return;var id=hit.project_id || hit.projectId || hit.id,store=catalogPicks(s),state=selectionState(s),kind=s.category,previous=store.entries[id];if(!id)return;
    if(previous && !version && !force)return;
    if(!previous && Object.keys(store.entries).length>=256){status(s,'Review the current selection before adding more projects.',true);return;}
    var entry={hit:copy(hit),loading:!version,error:null,versionId:version && version.id || previous && previous.explicit && previous.versionId || null,explicit:!!version || !!(previous && previous.explicit)};store.entries[id]=entry;
    function valid(){return live(s) && s.selections===state && inventoryContext(s)===state.key && store.entries[id]===entry;}
    function finish(selected){entry.loading=false;entry.versionId=selected.id;entry.version=copy(selected);entry.error=null;syncCatalogSelection(s,store,kind);}
    if(version){finish(version);return;}
    syncCatalogSelection(s,store,kind);
    request(s,'/versions?projectId='+encodeURIComponent(id)+'&kind='+encodeURIComponent(kind)).then(function(data){if(!valid())return;var versions=data.versions || [],chosen=entry.explicit?versions.find(function(v){return v.id===entry.versionId;}):versions.find(function(v){return v.version_type==='release';}) || versions[0];if(!chosen || !chosen.id)throw new Error(entry.explicit?'The selected version is no longer compatible. Choose another version.':'No compatible version is available.');finish(chosen);}).catch(function(error){if(!valid())return;entry.loading=false;entry.error=error.message || 'Compatible versions could not be loaded.';syncCatalogSelection(s,store,kind);});
  }
  function catalogFooter(s) {var store=catalogPicks(s);pickCount(s,catalogSelectionCount(s,store));selectionFooter(s,'Review selected ('+Object.keys(store.entries).length+')',function(){basket(s);},Object.keys(store.entries).length);}
  function paintBasket(s,store) {
    var list=s.pane.querySelector('[data-iw-basket-list]');if(!list)return;
    var active=document.activeElement,focusAttribute=['data-iw-edit-pick','data-iw-remove-pick','data-iw-retry-pick'].find(function(attribute){return list.contains(active) && active.hasAttribute(attribute);}),focusId=focusAttribute && active.getAttribute(focusAttribute),oldOrder=Array.from(list.querySelectorAll('[data-iw-remove-pick]')).map(function(b){return b.dataset.iwRemovePick;}),oldIndex=oldOrder.indexOf(focusId),scroll=s.pane.scrollTop;list.textContent='';
    var entries=Object.entries(store.entries);entries.forEach(function(pair){var id=pair[0],entry=pair[1],li=node(s,'li',null,'iw-basket-row',list),header=node(s,'div',null,'iw-project-header',li);logo(s,entry.hit,entry.hit.title || entry.hit.name,header);node(s,'strong',entry.hit.title || entry.hit.name || id,null,header);
      node(s,'p',entry.loading?'Finding a compatible version…':entry.error || ((entry.version && (entry.version.version_number || entry.version.name) || entry.versionId)+' · '+(entry.version && entry.version.version_type || 'release')),entry.error?'iw-error':'__hx-note',li);
      var a=node(s,'div',null,'iw-toolbar',li);var edit=button(s,'Choose version',function(){project(s,entry.hit,false,{versionId:entry.versionId,optional:store.optional,selectionBack:true});},a);edit.dataset.iwEditPick=id;edit.dataset.iwUnavailable=String(!!entry.loading);edit.disabled=s.busy || entry.loading;
      var remove=button(s,'Unselect',function(){delete store.entries[id];paintBasket(s,store);},a);remove.dataset.iwRemovePick=id;remove.disabled=s.busy;
      if(entry.error){var retry=button(s,'Retry',function(){acquirePick(s,entry.hit,null,true);},a);retry.dataset.iwRetryPick=id;retry.disabled=s.busy;}
    });
    if(!entries.length)node(s,'li','No projects selected. Return to Browse to pick compatible content.',null,list);
    var count=s.pane.querySelector('[data-iw-pick-count]');if(count)count.textContent=catalogSelectionCount(s,store);
    var resolve=s.screen.el.querySelector('[data-iw-resolve-selection]');if(resolve){resolve.dataset.iwUnavailable=String(!entries.length || entries.some(function(pair){return pair[1].loading || pair[1].error || !pair[1].versionId;}));resolve.disabled=s.busy || !!contentMutationReason(s) || resolve.dataset.iwUnavailable==='true';}
    if(focusAttribute){var target=Array.from(list.querySelectorAll('['+focusAttribute+']')).find(function(b){return b.getAttribute(focusAttribute)===focusId && !b.disabled;});if(!target){var next=entries[Math.min(Math.max(oldIndex,0),entries.length-1)];if(next)target=Array.from(list.querySelectorAll('[data-iw-remove-pick]')).find(function(b){return b.dataset.iwRemovePick===next[0] && !b.disabled;});}target=target || s.pane.querySelector('[data-iw-basket-back]');if(target)target.focus({preventScroll:true});s.pane.scrollTop=scroll;}
  }
  function basket(s) {
    var store=catalogPicks(s),kind=s.category;clear(s);s.selectionView='basket';heading(s,'Review selected content');targetContext(s);pickCount(s,catalogSelectionCount(s,store));
    var a=toolbar(s),back=button(s,'Back to Browse',function(){browse(s);},a,false,'data-iw-basket-back');button(s,'Clear selection',function(){store.entries=Object.create(null);paintBasket(s,store);},a,false,'data-iw-clear-selection');
    var optional=node(s,'label',null,'iw-check'),input=node(s,'input',null,null,optional);input.type='checkbox';input.checked=store.optional;input.dataset.iwBasketOptional='';input.setAttribute('aria-label','Include optional dependencies');input.addEventListener('change',function(){store.optional=input.checked;});node(s,'span','Include optional dependencies',null,optional);
    node(s,'p','Required dependencies are automatic. The next review checks these projects together and reuses matching installed files.','__hx-note');node(s,'ul',null,'iw-list').dataset.iwBasketList='';
    var resolve=button(s,'Resolve and review',function(){var snapshot=Object.entries(store.entries);if(snapshot.some(function(pair){return pair[1].loading || pair[1].error || !pair[1].versionId;}))return;planSelections(s,snapshot.map(function(pair){return {projectId:pair[0],versionId:pair[1].versionId};}),store.optional,function(){basket(s);},function(){snapshot.forEach(function(pair){if(store.entries[pair[0]]===pair[1])delete store.entries[pair[0]];});syncCatalogSelection(s,store,kind);});},s.screen.el.querySelector('.__hx-foot'),true,'data-iw-resolve-selection');resolve.dataset.iwFlowFooter='';resolve.dataset.iwLockedControl='';paintBasket(s,store);refreshFooter(s);back.focus({preventScroll:true});
  }
  async function content(s) {
    if(gateDraft(s,function(){content(s);}))return false;
    var scan=++s.inventoryScan,context=inventoryContext(s),root=s.instance.ownership && s.instance.ownership.root;s.inventory=null;s.inventoryError=null;installed(s);status(s,'Scanning installed files…');var epoch=s.epoch;
    function matches(data){return live(s) && s.inventoryScan===scan && inventoryContext(s)===context && (!data || !data.ownership || !data.ownership.root || data.ownership.root===root);}
    try {var data=await request(s,'/content');if(!matches(data))return false;refreshInstanceState(s,data);s.inventory=data;if(s.epoch!==epoch){if(['mods','resourcepacks','shaderpacks'].includes(s.category))refreshContentControls(s);return false;}installed(s);if(jobs[s.id] && !['succeeded','failed','complete'].includes(jobs[s.id].status))poll(s,jobs[s.id].id);return true;}
    catch(e){if(!matches())return false;s.inventoryError=e;if(s.epoch===epoch)fail(s,e,epoch);else if(['mods','resourcepacks','shaderpacks'].includes(s.category)){refreshContentControls(s);var notice=s.pane.querySelector('[data-iw-scan-error]') || node(s,'p',null,'iw-error');notice.setAttribute('data-iw-scan-error','');notice.setAttribute('role','alert');notice.textContent=e.message || String(e);}return false;}
  }
  function installed(s) {
    clear(s);heading(s,categories.find(function(c){return c[0]===s.category;})[1]);ownership(s);var blocked=contentMutationReason(s),actions=toolbar(s);
    button(s,'Browse Modrinth',function(){browse(s);},actions,false,'data-iw-browse');
    var imp=button(s,'Import local files',function(){importFiles(s);},actions,false,'data-iw-import');imp.disabled=!!blocked || s.busy;
    if(window.MCLauncher && typeof window.MCLauncher.openFileDialog==='function'){var native=button(s,'Use native file picker',function(){nativeImport(s,native);},actions,false,'data-iw-native-import');native.disabled=!!blocked || s.busy;}
    button(s,'Refresh',function(){content(s);},actions);
    if(!s.inventory){refreshFooter(s);return;}
    if(s.category==='resourcepacks'){var active=button(s,'Active resource packs',function(){activation(s);},actions,false,'data-iw-active-packs');active.disabled=!!blocked;}
    if(s.category==='shaderpacks')node(s,'p','Installed shaders require a compatible renderer in Minecraft. Installing a file does not activate a shader.','__hx-note');
    var problems=(s.inventory.problems || []).filter(function(p){var kind=String(p.path || '').replace(/\\/g,'/').split('/')[0];return !['mods','resourcepacks','shaderpacks'].includes(kind) || kind===s.category;});if(problems.length)status(s,problems.map(contentProblemMessage).join('\n'),true);
    var items=(s.inventory.items || []).filter(function(i){return i.kind===s.category;}),list=node(s,'ul',null,'iw-list');list.setAttribute('aria-label','Installed '+s.category);installedPickView(s,items,list);
    if(s.category==='mods'){var repair=button(s,'Repair dependencies',function(){repairDependencies(s);},actions,false,'data-iw-repair-dependencies');repair.dataset.iwLockedControl='';repair.dataset.iwUnavailable=String(!items.some(function(i){return i.enabled;}));}
    var managed=items.filter(function(i){return i.projectId;});if(managed.length){var update=button(s,'Check all updates',function(){updates(s,managed);},actions);update.disabled=!!blocked;}
    refreshFooter(s);
  }
  function contentProblemMessage(problem) {
    var message=problem.message || problem.error || String(problem);
    if(problem.code==='unknown_metadata' && problem.sourceFormat) {
      var format={fabric:'Fabric',quilt:'Quilt',forge:'Forge',neoforge:'NeoForge'}[problem.sourceFormat] || problem.sourceFormat;
      message=format+' metadata could not be inspected.';
    }
    return (problem.path?problem.path+': ':'')+message;
  }
  function detailInstalled(s,item) {
    clear(s);projectHeading(s,item,item.name || item.filename);node(s,'p',(item.version || 'Unknown version')+' · '+(item.enabled?'Enabled':'Disabled'),'__hx-note');node(s,'p',item.path || item.filename,'__hx-note');
    if(item.modIds && item.modIds.length)node(s,'p','Provided mod IDs: '+item.modIds.join(', '),'__hx-note');
    if(item.requiredBy && item.requiredBy.length)node(s,'p','Required by: '+item.requiredBy.map(function(id){var match=(s.inventory.items || []).find(function(i){return i.id===id || i.versionId===id || i.projectId===id;});return match?match.name || match.filename:id;}).join(', '),'__hx-note');
    (item.warnings || []).forEach(function(w){node(s,'p',w.message || String(w),'__hx-note');});
    (item.metadataProblems || []).forEach(function(problem){node(s,'p',contentProblemMessage(problem),'__hx-note');});
    var blocked=contentMutationReason(s);if(blocked)node(s,'p',blocked,'__hx-note');var actions=toolbar(s);
    button(s,'Back',function(){installed(s);},actions);
    var toggle=button(s,item.enabled?'Disable':'Enable',function(){previewSelected(s,item.enabled?'disable':'enable',[item],function(){detailInstalled(s,item);});},actions);toggle.disabled=!!blocked;
    var remove=button(s,'Remove',function(){previewSelected(s,'remove',[item],function(){detailInstalled(s,item);});},actions);remove.disabled=!!blocked;
    if(item.projectId){var update=button(s,'Check update',function(){project(s,Object.assign({},item,{project_id:item.projectId,title:item.name}),true);},actions);update.disabled=!!blocked;}
    if(s.category==='shaderpacks')shaderActivation(s,item,actions);
    if(s.category==='resourcepacks')button(s,'Choose active resource packs',function(){activation(s);},actions).disabled=!!blocked;
  }
  function shaderActivation(s,item,actions) {
    var epoch=s.epoch,data=null,pending=false,region=node(s,'div'),use=button(s,'Use shader',function(){if(!data || use.disabled)return;activationReview(s,'shaderpacks',{id:item.id,revision:data.revision},[item.name || item.filename],function(){detailInstalled(s,item);});},actions,false,'data-iw-use-shader');use.disabled=true;
    async function load(){if(pending || !live(s) || s.epoch!==epoch)return;pending=true;region.textContent='Reading renderer settings…';try{data=await request(s,'/tools?kind=shaderpacks');if(!live(s)||s.epoch!==epoch)return;region.textContent='';refreshInstanceState(s,data);use.disabled=!(data.capabilities || []).includes('activate') || !!contentMutationReason(s) || !item.enabled;if(data.activeId===item.id)node(s,'p','This shader is selected in the renderer configuration.','__hx-note',region);if(data.message)node(s,'p',data.message,'__hx-note',region);}catch(e){data=null;readRecovery(s,e,epoch,load,null,region);}finally{pending=false;}}load();
  }
  async function activation(s,reset) {
    if(s.busy)return;
    clear(s);heading(s,'Active resource packs');status(s,'Reading Minecraft resource settings…');var epoch=s.epoch;
    try{var data=await request(s,'/tools?kind=resourcepacks');if(!live(s)||s.epoch!==epoch)return;refreshInstanceState(s,data);var items=(s.inventory.items || []).filter(function(i){return i.kind==='resourcepacks' && i.enabled;}),key=inventoryContext(s)+'|'+s.inventory.revision,draft=s.activationDraft;
      if(reset || !draft || draft.key!==key){var selected=(data.activeIds || []).filter(function(id){return items.some(function(i){return i.id===id;});});draft={key:key,revision:data.revision,selected:selected,ordered:selected.concat(items.filter(function(i){return !selected.includes(i.id);}).map(function(i){return i.id;}))};s.activationDraft=draft;}draft.items=items;draft.data=data;renderActivation(s,draft);
    }catch(e){readRecovery(s,e,epoch,function(){activation(s,reset);},function(){installed(s);});}
  }
  function renderActivation(s,draft) {
    if(draft.key!==inventoryContext(s)+'|'+s.inventory.revision){s.activationDraft=null;activation(s);return;}clear(s);heading(s,'Active resource packs');node(s,'p','Select packs and arrange their priority. The top pack overrides lower packs; vanilla stays available.','__hx-note');var items=draft.items,data=draft.data,list=node(s,'ul',null,'iw-list');
    function draw(focusId,attribute){var scroll=s.pane.scrollTop;list.textContent='';draft.ordered.forEach(function(id,index){var item=items.find(function(i){return i.id===id;}),name=item.name || item.filename,li=node(s,'li',null,'iw-fallback-row',list);li.dataset.packId=id;var label=node(s,'label',null,'iw-check',li),box=node(s,'input',null,null,label);box.type='checkbox';box.dataset.iwLockedControl='';box.checked=draft.selected.includes(id);box.setAttribute('aria-label','Use '+name);node(s,'span',name,null,label);box.addEventListener('change',function(){draft.selected=box.checked?draft.selected.concat(id):draft.selected.filter(function(v){return v!==id;});});var a=node(s,'div',null,'iw-toolbar',li);var up=button(s,'Up',function(){draft.ordered.splice(index-1,0,draft.ordered.splice(index,1)[0]);draw(id,'data-iw-up');},a,false,'data-iw-up');up.setAttribute('aria-label','Move '+name+' up');up.dataset.iwLockedControl='';up.dataset.iwUnavailable=String(index===0);up.disabled=index===0;var down=button(s,'Down',function(){draft.ordered.splice(index+1,0,draft.ordered.splice(index,1)[0]);draw(id,'data-iw-down');},a,false,'data-iw-down');down.setAttribute('aria-label','Move '+name+' down');down.dataset.iwLockedControl='';down.dataset.iwUnavailable=String(index===draft.ordered.length-1);down.disabled=index===draft.ordered.length-1;});refreshContentControls(s);if(focusId){var li=Array.from(list.children).find(function(n){return n.dataset.packId===focusId;}),target=li && li.querySelector('['+attribute+']:not(:disabled)') || li && li.querySelector('button:not(:disabled)') || li && li.querySelector('input');if(target)target.focus({preventScroll:true});s.pane.scrollTop=scroll;}}draw();
    var actions=toolbar(s);button(s,'Back',function(){installed(s);},actions);var reviewButton=button(s,'Review active packs',function(){var ids=draft.ordered.filter(function(id){return draft.selected.includes(id);});activationReview(s,'resourcepacks',{ids:ids,revision:draft.revision},ids.map(function(id){var item=items.find(function(i){return i.id===id;});return item.name || item.filename;}),function(){renderActivation(s,draft);});},s.screen.el.querySelector('.__hx-foot'),true,'data-iw-review-active');reviewButton.dataset.iwFlowFooter='';reviewButton.disabled=!(data.capabilities || []).includes('activate') || !!contentMutationReason(s);if(draft.revision!==data.revision){node(s,'p','Saved Minecraft settings changed. Your selection is preserved; reload the saved selection before applying.','iw-error').setAttribute('role','alert');button(s,'Reload saved selection',function(){activation(s,true);},actions);}if(data.message)node(s,'p',data.message,'__hx-note');refreshFooter(s);
  }
  function activationReview(s,kind,payload,names,back) {
    clear(s);heading(s,kind==='resourcepacks'?'Use resource packs in Minecraft':'Use shader');node(s,'p',kind==='resourcepacks'?'The following ordered pack set replaces Minecraft’s current pack selection. Unselected packs remain installed.':'The shader is selected in the supported renderer configuration. This is separate from installing its file.','__hx-note');var list=node(s,'ol');names.forEach(function(name){node(s,'li',name,null,list);});if(!names.length)node(s,'p','Only vanilla resources will be active.','__hx-note');var a=toolbar(s);button(s,'Back',back || function(){installed(s);},a);var apply=button(s,'Apply selection',function(){toolAction(s,kind,'activate',payload,null,function(){if(kind==='resourcepacks')s.activationDraft=null;});},s.screen.el.querySelector('.__hx-foot'),true,'data-iw-activate');apply.dataset.iwFlowFooter='';apply.dataset.iwLockedControl='';refreshFooter(s);
  }
  function immediateReview(s,title,route,payload) {
    clear(s);heading(s,title);node(s,'p','This changes installation files immediately. General Cancel cannot undo it. The launcher checks dependencies and keeps recoverable transaction history.','__hx-note');var a=toolbar(s);button(s,'Back',function(){installed(s);},a);button(s,title.split(' ')[0],function(){mutate(s,route,payload);},a,true);
  }
  async function mutate(s,route,payload) {
    if(s.busy || contentMutationReason(s))return;s.busy=true;refreshFooter(s);var epoch=s.epoch;
    try {var data=await request(s,route,payload);s.committed=true;if(data.jobId)jobs[s.id]={id:data.jobId,status:'queued'};if(!live(s)||s.epoch!==epoch)return;var picks=installedPicks(s);(payload.ids || (payload.id?[payload.id]:[])).forEach(function(id){delete picks.ids[id];});if(data.jobId){progress(s,data.jobId);}else {s.inventory=data.items?data:null;var refreshed=await content(s);if(refreshed && live(s))status(s,'The installation files were updated.');}}
    catch(e){fail(s,e,epoch);if(live(s)&&s.epoch===epoch && e.details && e.details.dependents){var a=toolbar(s),names=e.details.dependents.map(function(d){return d.name || d.id;});node(s,'p','Also affected: '+names.join(', '),'__hx-note');var selected=(s.inventory.items || []).filter(function(i){return (payload.ids || [payload.id]).includes(i.id);});if(selected.length)button(s,'Review affected files',function(){previewSelected(s,route==='/remove'?'remove':payload.enabled?'enable':'disable',selected,function(){installed(s);});},a);}}finally{s.busy=false;refreshFooter(s);}
  }
  function importFiles(s) {
    if(contentMutationReason(s)||s.busy)return;
    var input=document.createElement('input');input.type='file';input.multiple=true;input.accept=s.category==='mods'?'.jar':'.zip,.jar';
    input.addEventListener('change',async function(){if(!input.files.length)return;var epoch=s.epoch,kind=s.category;try{if(Array.from(input.files).reduce(function(sum,file){return sum+file.size;},0)>32*1024*1024)throw new Error('Browser import is limited to 32 MiB total. Use the native file picker for files up to 512 MiB each.');var files=[];for(var file of input.files){var data=await new Promise(function(resolve,reject){var r=new FileReader();r.onload=function(){resolve(String(r.result).split(',')[1]);};r.onerror=function(){reject(new Error('The selected file could not be read.'));};r.readAsDataURL(file);});files.push({name:file.name,data:data});}if(!live(s)||s.epoch!==epoch)return;clear(s);heading(s,'Import local files');files.forEach(function(f){node(s,'p',f.name,'__hx-note');});node(s,'p','The launcher inspects metadata and checks conflicts before committing these files.','__hx-note');var a=toolbar(s);button(s,'Back',function(){installed(s);},a);button(s,'Import',function(){mutate(s,'/import',{kind:kind,files:files,revision:s.inventory.revision});},a,true);}catch(e){fail(s,e,epoch);}});
    input.click();
  }
  async function nativeImport(s,trigger){if(s.busy || contentMutationReason(s))return;var epoch=s.epoch,kind=s.category;s.busy=true;trigger.disabled=true;refreshFooter(s);try{var path=await window.MCLauncher.openFileDialog({mode:'open_file',path:s.instance.ownership.root,filters:kind==='mods'?['.jar']:['.zip','.jar']});if(!live(s)||s.epoch!==epoch)return;if(!path){status(s,'File selection was cancelled.');return;}clear(s);heading(s,'Import local file');node(s,'p',String(path),'__hx-note');node(s,'p','The launcher validates this local file before committing it. The native picker supports files up to 512 MiB.','__hx-note');var a=toolbar(s);button(s,'Back',function(){installed(s);},a);button(s,'Import',function(){mutate(s,'/import',{kind:kind,path:String(path),revision:s.inventory.revision});},a,true,'data-iw-native-import-review');}catch(e){fail(s,e,epoch);}finally{s.busy=false;trigger.disabled=!!contentMutationReason(s);refreshFooter(s);}}
  function browse(s,query,offset,selection,previous) {
    s.catalogs=s.catalogs || Object.create(null);
    var saved=s.catalogs[s.category] || {query:'',offset:0,previous:[]};
    if(query===undefined){query=saved.query;offset=saved.offset;previous=saved.previous;}
    offset=offset || 0;
    var catalog={query:query || '',offset:offset,previous:previous || (offset===0?[]:saved.previous)};s.catalogs[s.category]=catalog;
    clear(s);s.selectionView='catalog';heading(s,'Browse '+categories.find(function(c){return c[0]===s.category;})[1]);targetContext(s);catalogFooter(s);
    var actions=toolbar(s);button(s,'Installed',function(){installed(s);},actions);var form=node(s,'div',null,'__hx-row'),lab=node(s,'label','Search Modrinth',null,form);var search;
    var nativeSearch=document.querySelector('main #versions_search_label') || document.querySelector('main label[id*=search]'),nativeInput=nativeSearch && nativeSearch.parentElement.querySelector('input');
    if(nativeInput && nativeInput.parentElement){var wrapper=nativeInput.parentElement.cloneNode(true);wrapper.querySelectorAll('[id]').forEach(function(n){n.removeAttribute('id');});wrapper.querySelectorAll('.results-tag,[role=status]').forEach(function(n){n.remove();});wrapper.querySelectorAll('.results-padding').forEach(function(n){n.classList.remove('results-padding');});search=wrapper.querySelector('input');search.id='__hx-modpack-search';search.value=query || '';search.placeholder='Search compatible content';search.setAttribute('aria-label','Search compatible content');var reset=wrapper.querySelector('.reset-button');if(!reset)reset=button(s,'Clear search',function(){browse(s,'',0,{start:0,end:0,direction:'none'});},actions,false,'data-iw-search-reset');else reset.addEventListener('click',function(){browse(s,'',0,{start:0,end:0,direction:'none'});});reset.setAttribute('data-iw-search-reset','');reset.setAttribute('aria-label','Clear content search');reset.hidden=!search.value;search.addEventListener('input',function(){reset.hidden=!search.value;});wrapper.style.cssText='width:100%;min-width:0';form.appendChild(wrapper);}else{search=node(s,'input',null,null,form);search.type='text';search.id='iw-search';search.value=query || '';search.placeholder='Search compatible content';var reset=button(s,'Clear search',function(){browse(s,'',0,{start:0,end:0,direction:'none'});},actions,false,'data-iw-search-reset');reset.setAttribute('aria-label','Clear content search');reset.hidden=!search.value;search.addEventListener('input',function(){reset.hidden=!search.value;});}lab.htmlFor=search.id;
    button(s,'Search',function(){browse(s,search.value,0);},actions);
    search.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();e.stopPropagation();browse(s,search.value,0,{start:search.selectionStart,end:search.selectionEnd,direction:search.selectionDirection});}});
    var timer;search.addEventListener('input',function(){clearTimeout(timer);timer=setTimeout(function(){if(live(s)&&s.epoch===epoch)browse(s,search.value,0,document.activeElement===search?{start:search.selectionStart,end:search.selectionEnd,direction:search.selectionDirection}:null);},350);});
    if(selection){search.focus();if(selection.start!==null)search.setSelectionRange(selection.start,selection.end,selection.direction);}
    status(s,'Searching compatible content…');var epoch=s.epoch,kind=s.category;
    request(s,'/catalog?kind='+encodeURIComponent(kind)+'&query='+encodeURIComponent(query || '')+'&offset='+offset).then(function(data){if(!live(s)||s.epoch!==epoch)return;status(s,(data.total_hits || 0)+' compatible results');var store=catalogPicks(s),hits=data.hits || [],pickActions=toolbar(s);
      button(s,'Select visible',function(){hits.forEach(function(hit){acquirePick(s,hit);});syncCatalogSelection(s,store,kind);},pickActions,false,'data-iw-select-visible').disabled=s.busy || !hits.length;
      button(s,'Clear selection',function(){store.entries=Object.create(null);syncCatalogSelection(s,store,kind);},pickActions,false,'data-iw-clear-selection');
      var list=node(s,'ul',null,'iw-list');hits.forEach(function(hit){var id=hit.project_id || hit.id,li=row(s,list,id,hit.title || hit.name,hit.description || '','Details',function(){catalog.scroll=s.pane.scrollTop;project(s,hit);},true,hit);pickCheckbox(s,li,id,hit.title || hit.name,store.entries[id],function(checked){if(checked)acquirePick(s,hit);else delete store.entries[id];syncCatalogSelection(s,store,kind);},true);});
      if(!hits.length)node(s,'p','No compatible results. Try another search.','__hx-note');var navigation=toolbar(s);if(catalog.previous.length)button(s,'Previous results',function(){var trail=catalog.previous.slice(),prior=trail.pop();browse(s,query,prior,undefined,trail);},navigation);if(hits.length && offset+hits.length<data.total_hits)button(s,'Next results',function(){browse(s,query,offset+hits.length,undefined,catalog.previous.concat(offset));},navigation);if(saved===catalog)s.pane.scrollTop=catalog.scroll || 0;else if(query===saved.query && offset===saved.offset)s.pane.scrollTop=saved.scroll || 0;else s.pane.scrollTop=0;syncCatalogSelection(s,store,kind);refreshFooter(s);
    }).catch(function(e){fail(s,e,epoch);});
  }
  function project(s,hit,updating,choice) {
    clear(s);s.selectionView='project';projectHeading(s,hit,hit.title || hit.name || hit.project_id);if(hit.description)node(s,'p',hit.description,'__hx-note');targetContext(s);var actions=toolbar(s);button(s,'Back',function(){updating?installed(s):choice && choice.selectionBack?basket(s):browse(s);},actions);status(s,'Loading compatible versions…');var epoch=s.epoch,id=hit.project_id || hit.projectId || hit.id,store=catalogPicks(s);catalogFooter(s);
    var slug=typeof hit.slug==='string' && /^[a-z0-9][a-z0-9_-]{0,127}$/i.test(hit.slug)?hit.slug:String(id || ''),type={mods:'mod',resourcepacks:'resourcepack',shaderpacks:'shader'}[s.category];if(type && /^[a-z0-9][a-z0-9_-]{0,127}$/i.test(slug)){var link=node(s,'a','View original project on Modrinth','external-link external-link-light external-link-wrappable',actions);link.dataset.iwProjectLink='';link.href='https://modrinth.com/'+type+'/'+encodeURIComponent(slug);link.target='_blank';link.rel='noopener noreferrer';}
    request(s,'/versions?projectId='+encodeURIComponent(id)+'&kind='+encodeURIComponent(s.category)).then(function(data){if(!live(s)||s.epoch!==epoch)return;status(s,'');var versions=data.versions || [];if(!versions.length){node(s,'p','No compatible versions are available for this installation.','__hx-note');return;}var preferred=choice && choice.versionId || store.entries[id] && store.entries[id].versionId,available=versions.find(function(v){return v.id===preferred;});if(preferred && !available){status(s,'The selected version is no longer compatible. Choose another version before reviewing it.',true);if(store.entries[id])store.entries[id].error='The selected version is no longer compatible. Choose another version.';}s.draft.contentVersion=available?available.id:(versions.find(function(v){return v.version_type==='release';}) || versions[0]).id;var selected=s.draft.contentVersion;picker(s,s.pane,'contentVersion','Compatible version',versions.map(function(v){return {value:v.id,label:(v.version_number || v.name || v.id)+' · '+(v.version_type || 'release')};}),function(){delete s.draft.contentVersion;});delete s.draft.contentVersion;
      // The content selection is independent of General's save draft.
      var trigger=s.pane.querySelector('#iw-contentVersion');trigger.parentElement.querySelectorAll('li button').forEach(function(b,i){b.addEventListener('click',function(){selected=versions[i].id;delete s.draft.contentVersion;refreshFooter(s);});});
      var installed=(s.inventory && s.inventory.items || []).filter(function(item){return item.projectId===id;});if(installed.length)node(s,'p','Installed: '+installed.map(function(item){return (item.version || 'Unknown version')+(item.enabled?'':' · Disabled');}).join(', '),'__hx-note');
      node(s,'p','Required dependencies are included automatically. Review the files before installing.','__hx-note');
      var optional=node(s,'label',null,'iw-check');var checkbox=node(s,'input',null,null,optional);checkbox.type='checkbox';checkbox.checked=!!(choice && choice.optional);node(s,'span','Include optional dependencies',null,optional);
      var pick=button(s,store.entries[id]?'Use this version in selection':'Add to selection',function(){acquirePick(s,hit,versions.find(function(v){return v.id===selected;}));labelButton(pick,'Use this version in selection');},toolbar(s),false,'data-iw-add-selection');pick.disabled=s.busy;
      var blocked=contentMutationReason(s),plan=button(s,updating?'Review update':'Review installation',function(){var selectedChoice={versionId:selected,optional:checkbox.checked};planSelections(s,[{projectId:id,versionId:selected}],checkbox.checked,function(){project(s,hit,updating,selectedChoice);});},toolbar(s),true,'data-iw-plan');plan.disabled=!!blocked;if(blocked)node(s,'p',blocked,'__hx-note').setAttribute('data-iw-content-reason','');
    }).catch(function(e){readRecovery(s,e,epoch,function(){project(s,hit,updating,choice);});});
  }
  async function updates(s,items,back) {
    if(s.busy || contentMutationReason(s))return;back=back || function(){installed(s);};clear(s);heading(s,'Check compatible updates');status(s,'Checking '+items.length+' managed '+(items.length===1?'project':'projects')+'…');var epoch=s.epoch;s.busy=true;refreshFooter(s);
    try {var data=await request(s,'/plan',{kind:s.category,updates:true,ids:items.map(function(i){return i.id;}),revision:s.inventory.revision});if(!live(s)||s.epoch!==epoch)return;s.plan=data;review(s,data,back);}catch(e){readRecovery(s,e,epoch,function(){updates(s,items,back);},back);}finally{s.busy=false;refreshFooter(s);}
  }
  async function repairDependencies(s,chosenIds,back) {
    if(s.busy || s.category!=='mods' || changed(s) || contentMutationReason(s))return;
    var scoped=chosenIds!==undefined,items=(s.inventory.items || []).filter(function(i){return i.kind==='mods' && i.enabled;}),ids=scoped && Array.isArray(chosenIds)?chosenIds.slice():null;
    if(scoped && (!ids || !ids.length || ids.some(function(id){return typeof id!=='string' || !items.some(function(i){return i.id===id;});})))return;
    if(!items.length)return;back=back || function(){installed(s);};var body={kind:'mods',repairDependencies:true,revision:s.inventory.revision};if(scoped)body.ids=ids;
    clear(s);heading(s,'Repair dependencies');targetContext(s);node(s,'p',(scoped?ids.length+' enabled selected mods':'All '+items.length+' enabled installed mods')+' will be checked for required dependencies. Review the proposed changes before installing.','__hx-note');status(s,'Checking required dependencies…');s.busy=true;refreshFooter(s);var epoch=s.epoch,context=inventoryContext(s);
    try{var data=await request(s,'/plan',body);if(!live(s) || s.epoch!==epoch || inventoryContext(s)!==context)return;s.plan=data;review(s,data,back,null,scoped?'selected':'installed');}
    catch(e){readRecovery(s,e,epoch,function(){repairDependencies(s,scoped?ids:undefined,back);},back);}
    finally{s.busy=false;refreshFooter(s);}
  }
  async function planSelections(s,selections,optional,back,onApplied) {
    if(s.busy || contentMutationReason(s))return;s.busy=true;status(s,'Checking compatibility and dependencies…');refreshFooter(s);var epoch=s.epoch;
    try {var data=await request(s,'/plan',{kind:s.category,selections:selections,includeOptional:optional,revision:s.inventory.revision});if(!live(s)||s.epoch!==epoch)return;s.plan=data;review(s,data,back,onApplied);}catch(e){fail(s,e,epoch);}finally{s.busy=false;refreshFooter(s);}
  }
  function reviewName(s,plan,value) {
    if(value && typeof value==='object'){if(value.filename)return value.name && value.name!==value.filename?value.name+' ('+value.filename+')':value.filename;if(value.name)return value.name;value=value.id || value.versionId || value.path;}
    var items=(s.inventory && s.inventory.items || []).concat(plan.files || []),matches=items.filter(function(i){return i.id===value;});
    if(!matches.length)matches=items.filter(function(i){return i.versionId===value || i.projectId===value;});
    var names=matches.map(function(i){return i.filename?i.name && i.name!==i.filename?i.name+' ('+i.filename+')':i.filename:i.name || i.path || i.id || i.versionId;}).filter(Boolean);
    return Array.from(new Set(names)).join(', ') || String(value == null?'Unidentified file':value);
  }
  function diagnosticKey(value) {
    if(Array.isArray(value))return value.map(diagnosticKey);
    if(value && typeof value==='object'){var ordered=Object.create(null);Object.keys(value).sort().forEach(function(key){ordered[key]=diagnosticKey(value[key]);});return ordered;}
    return value;
  }
  function diagnosticDetails(s,plan,entry) {
    var labels={itemId:'File',otherItemId:'Related file',itemIds:'Files',dependency:'Dependency',modId:'Module',requirement:'Requires',requirements:'Requires',versionId:'Version',selectedVersionId:'Selected version',dependencyVersionId:'Dependency version',projectId:'Project',requiredBy:'Required by',reference:'Reference',filename:'Filename',path:'Path',cycle:'Dependency cycle',error:'Error',problems:'Metadata details',renderers:'Renderers'};
    function text(value,key){if(value==null)return 'Unknown';if(key==='requirement' && typeof value==='object')return JSON.stringify(value);if(Array.isArray(value))return value.map(function(v){return text(v,key);}).join(key==='cycle'?' → ':', ') || 'None';if(typeof value==='object')return Object.keys(value).sort().map(function(k){return (labels[k] || k)+': '+text(value[k],k);}).join('; ');if(/^(itemId|otherItemId|itemIds|versionId|selectedVersionId|dependencyVersionId|projectId|requiredBy|reference|cycle)$/.test(key))return reviewName(s,plan,value);return String(value);}
    return Object.keys(entry).filter(function(key){return !['message','reason','code'].includes(key);}).sort().map(function(key){return (labels[key] || key.replace(/([a-z])([A-Z])/g,'$1 $2'))+': '+text(entry[key],key);}).join(' · ');
  }
  function reviewMessages(s,plan,entries,title,error,parent) {
    if(!entries || !entries.length)return;
    var groups=new Map();entries.forEach(function(entry){var record=typeof entry==='string'?{message:entry}:entry || {message:'Compatibility information is unavailable.'},message=record.message || record.reason || record.code || 'Compatibility information is unavailable.',key=JSON.stringify([record.code || '',message]),group=groups.get(key);if(!group){group={message:message,entries:new Map()};groups.set(key,group);}var exact=JSON.stringify(diagnosticKey(record)),item=group.entries.get(exact);if(item)item.count++;else group.entries.set(exact,{record:record,count:1});});
    node(s,'h4',title+(error?'':' ('+entries.length+')'),null,parent);var detailParent=parent;
    if(!error){var toggle=button(s,'Show warning details',function(){details.hidden=!details.hidden;toggle.setAttribute('aria-expanded',String(!details.hidden));labelButton(toggle,details.hidden?'Show warning details':'Hide warning details');},parent || s.pane,false,'data-iw-warning-toggle');toggle.setAttribute('aria-expanded','false');var details=node(s,'div',null,null,parent);details.dataset.iwWarningDetails='';details.id='iw-warnings-'+s.sequence+'-'+s.epoch+'-'+s.pane.querySelectorAll('[data-iw-warning-details]').length;details.hidden=true;toggle.setAttribute('aria-controls',details.id);detailParent=details;}
    var list=node(s,'ul',null,error?'iw-error':'__hx-note',detailParent);if(error)list.setAttribute('role','alert');
    groups.forEach(function(group){var li=node(s,'li',null,null,list),entries=Array.from(group.entries.values());function detail(item){var description=diagnosticDetails(s,plan,item.record);return description+(item.count>1?' (reported '+item.count+' times)':'');}if(entries.length===1){var description=detail(entries[0]);li.textContent=group.message+(description?' — '+description:'');}else{node(s,'span',group.message,null,li);var details=node(s,'ul',null,null,li);entries.forEach(function(item){node(s,'li',detail(item) || 'No additional details.',null,details);});}});
  }
  function failureDetails(s,error) {
    var old=s.pane.querySelector('[data-iw-error-details]');if(old)old.remove();
    var details=error.details,entries=Array.isArray(details)?details:details && (details.conflicts || details.problems);
    if(!Array.isArray(entries) || !entries.length)return;
    var box=node(s,'div');box.dataset.iwErrorDetails='';reviewMessages(s,s.plan || {},entries,'What needs attention',true,box);
    var missing=entries.find(function(entry){return entry && /missing_dependency|required_dependency/.test(entry.code || '');});
    if(missing && s.category==='mods'){var query=String(missing.dependency || missing.modId || '').replace(/_/g,' ');button(s,'Browse required mods',function(){if(!s.inventory && (!s.inventoryScan || s.inventoryError))content(s);browse(s,query,0);},box,false,'data-iw-browse-required');}
  }
  function planHasChanges(plan) {return ['files','enable','remove'].some(function(key){return (plan[key] || []).length;}) || !!Object.keys(plan.reuseMetadata || {}).length;}
  function review(s,plan,back,onApplied,repairScope) {
    clear(s);heading(s,'Review installation changes');targetContext(s);var files=plan.files || [],hasChanges=planHasChanges(plan),conflicts=!!(plan.conflicts && plan.conflicts.length),repair=plan.dependencyRepair,unidentified=repair && repair.unidentified || [],known=files.every(function(file){return Number.isFinite(file.size) && file.size>=0;}),bytes=files.reduce(function(sum,file){return sum+(Number.isFinite(file.size)?file.size:0);},0);
    if(repair){node(s,'p','Checked '+repair.checked+' enabled '+(repairScope || 'installed')+' '+(repair.checked===1?'mod':'mods')+'. Required dependencies only; installed versions are kept.','__hx-note').dataset.iwRepairScope='';if(unidentified.length){node(s,'h4','Unidentified files ('+unidentified.length+')');node(s,'p','These files could not be identified, so their dependencies could not be checked. Their existing files stay in place.','__hx-note');var unknownList=node(s,'ul',null,'__hx-note');unknownList.dataset.iwRepairUnidentified='';unidentified.forEach(function(item){node(s,'li',reviewName(s,plan,item),null,unknownList);});}}
    node(s,'p',files.length+' '+(files.length===1?'download':'downloads')+(files.length?' · '+(known?fileSize(bytes):'Total size unavailable'):(plan.reuse || []).length?' · Existing files will be reused.':''),'__hx-note');
    node(s,'p',conflicts?'Resolve the conflicts below before applying changes.':hasChanges?'Files change when you apply this review. Completed changes remain when the editor is closed.':repair?unidentified.length?'No dependency changes were proposed for the identified mods. Some files could not be checked.':'No missing required dependencies were found in the identified mods.':'This content is already installed. No changes needed.','__hx-note');
    [['files','Install'],['reuse','Reuse existing'],['enable','Enable required'],['remove','Replace or remove']].forEach(function(pair){if(!plan[pair[0]] || !plan[pair[0]].length)return;node(s,'h4',pair[1]);var list=node(s,'ul',null,'iw-list');plan[pair[0]].forEach(function(file){if(pair[0]!=='files'){node(s,'li',reviewName(s,plan,file),null,list);return;}var li=node(s,'li',null,'iw-review-file',list);logo(s,file,file.name || file.filename,li);var copy=node(s,'div',null,'iw-row-copy',li);node(s,'strong',reviewName(s,plan,file),null,copy);node(s,'p',(file.version?file.version+' · ':'')+fileSize(file.size)+(file.userRequested===false?' · Dependency':' · Selected by you'),null,copy);if(file.requiredBy && file.requiredBy.length)node(s,'p','Required by: '+file.requiredBy.map(function(id){return reviewName(s,plan,id);}).join(', '),null,copy);});});
    reviewMessages(s,plan,plan.conflicts,'Conflicts',true);
    reviewMessages(s,plan,plan.warnings,'Warnings',false);
    var a=toolbar(s);button(s,'Back',back || function(){installed(s);},a);
    var label=conflicts?'Resolve conflicts':!hasChanges?repair && unidentified.length?'No changes proposed':'No changes needed':!files.length?((plan.enable || []).length || (plan.remove || []).length?'Apply changes':'Use existing files'):(plan.remove || []).length?'Apply update':'Install';
    var apply=button(s,label,async function(){
      if(s.busy || !hasChanges || contentMutationReason(s))return;s.busy=true;apply.disabled=true;var epoch=s.epoch,kind=s.category;
      try{var data=await request(s,'/apply',{planId:plan.planId});if(!data.jobId)throw new Error('The installer did not return an operation. Refresh installed files before retrying.');jobs[s.id]={id:data.jobId,kind:kind,status:'queued'};s.committed=true;if(onApplied)onApplied();if(!live(s))return;if(s.epoch===epoch)progress(s,data.jobId);else poll(s,data.jobId);}
      catch(e){fail(s,e,epoch);apply.disabled=false;}finally{s.busy=false;refreshFooter(s);}
    },s.screen.el.querySelector('.__hx-foot'),true,'data-iw-apply');apply.dataset.iwFlowFooter='';apply.disabled=!hasChanges || !plan.planId || !!contentMutationReason(s) || !!(plan.conflicts && plan.conflicts.length);if(!hasChanges)button(s,'Installed files',function(){content(s);},a);
  }
  function jobStatus(job){var control=job.controlStatus;if(['paused','cancelling','cancelled'].includes(control))return control;if(control==='error')return 'failed';return job.status==='done'?'complete':job.status==='error'?'failed':job.status;}
  function jobTerminal(job){return ['succeeded','complete','failed','cancelled','idle'].includes(jobStatus(job));}
  function ownedJob(data,id,kind){var job=data.job || {};if(job.id && job.id!==id || job.jobId && job.jobId!==id)throw new Error('The launcher returned a different operation. Refresh this installation before trying again.');return Object.assign({kind:kind},job,{id:id,jobId:id,status:jobStatus(job)});}
  function rememberJob(s,state,job){state.last=job;state.terminal=jobTerminal(job);s.instance.latestJob=job;jobs[s.id]=job;paintJob(s,job);refreshFooter(s);}
  function progress(s,id) {var latest=s.instance.latestJob,known=s.jobPoll && s.jobPoll.id===id && s.jobPoll.last || latest && latest.id===id && latest || jobs[s.id] && jobs[s.id].id===id && jobs[s.id] || {},kind=known.kind || (s.category==='general'?'mods':s.category);if(!categories.some(function(c){return c[0]===kind && kind!=='general';}))kind='mods';showCategory(s,kind);clear(s);s.pane.dataset.iwJobId=id;heading(s,'Installing content');targetContext(s);status(s,'Preparing installation changes…');node(s,'p','You can close this editor while installation continues. Reopen it to see the result.','__hx-note').dataset.iwJobNote='';refreshFooter(s);if((!s.jobPoll || s.jobPoll.id!==id) && latest && latest.id===id)s.jobPoll={id:id,kind:kind,inFlight:false,terminal:jobTerminal(latest),last:latest,generation:0};if(s.jobPoll && s.jobPoll.id===id && s.jobPoll.last)paintJob(s,s.jobPoll.last);poll(s,id);}
  async function controlJob(s,id,action){
    var state=s.jobPoll,epoch=s.epoch,error;if(!live(s) || !state || state.id!==id || state.controlPending || state.terminal || s.pane.dataset.iwJobId!==id)return;
    var job=state.last || {},allowed=action==='pause'?job.canPause:action==='resume'?job.canResume:job.canCancel;if(allowed!==true)return;
    state.controlPending=true;state.generation=(state.generation || 0)+1;clearTimeout(s.poll);paintJob(s,job);
    try{var data=await request(s,'/job/control',{jobId:id,action:action});if(!live(s) || s.jobPoll!==state)return;rememberJob(s,state,ownedJob(data,id,state.kind));}
    catch(e){error=e;}
    finally{state.controlPending=false;if(live(s) && s.jobPoll===state){if(state.last)paintJob(s,state.last);if(error && s.epoch===epoch && s.pane.dataset.iwJobId===id)fail(s,error,epoch);refreshFooter(s);if(!state.terminal)s.poll=setTimeout(function(){poll(s,id);},700);}}
  }
  function paintJobControls(s,job,terminal){
    var state=s.jobPoll,controls=s.pane.querySelector('[data-iw-job-controls]');if(!controls){controls=node(s,'div');controls.dataset.iwJobControls='';var a=node(s,'div',null,'iw-toolbar',controls);button(s,'Pause',function(){var latest=s.jobPoll && s.jobPoll.last || {};controlJob(s,job.id,jobStatus(latest)==='paused' || latest.canResume===true?'resume':'pause');},a,false,'data-iw-job-pause');button(s,'Cancel download',function(){controlJob(s,job.id,'cancel');},a,false,'data-iw-job-cancel');var reason=node(s,'p','','__hx-note',controls);reason.dataset.iwJobControlReason='';reason.id='iw-job-control-reason-'+s.sequence;controls.querySelectorAll('button').forEach(function(b){b.setAttribute('aria-describedby',reason.id);});}
    var paused=jobStatus(job)==='paused' || job.canResume===true,pending=!!(state && state.controlPending),toggle=controls.querySelector('[data-iw-job-pause]'),cancel=controls.querySelector('[data-iw-job-cancel]');labelButton(toggle,paused?'Resume':'Pause');toggle.disabled=terminal || pending || (paused?job.canResume:job.canPause)!==true;cancel.disabled=terminal || pending || job.canCancel!==true;
    var reason=controls.querySelector('[data-iw-job-control-reason]');reason.textContent=pending?'Requesting download control…':terminal?jobStatus(job)==='cancelled'?'Cancelled. Verified downloads are kept; unverified temporary files are removed.':'This operation has ended.':jobStatus(job)==='cancelling'?'Cancelling this download and removing unverified temporary files…':job.controlReason || (paused?'Acquisition is paused. Resume to continue, or cancel this download.':toggle.disabled && cancel.disabled?'Download controls are unavailable during this step.':'Pause keeps verified downloads. Cancel stops this operation before its changes are applied.');
  }
  function paintJob(s,job) {
    if(!live(s) || s.pane.dataset.iwJobId!==job.id)return;
    var state=jobStatus(job),terminal=jobTerminal(job),success=['succeeded','complete'].includes(state),title=s.pane.querySelector('h3');if(title)title.textContent=success?'Installation complete':state==='failed'?'Installation failed':state==='cancelled'?'Installation cancelled':state==='paused'?'Installation paused':state==='cancelling'?'Cancelling installation':'Installing content';
    var note=s.pane.querySelector('[data-iw-job-note]');if(note && terminal)note.textContent=success?'The content changes are complete. View installed files to manage them.':job.status==='failed'?'Review the error and installed files before trying again.':'View installed files to check the final state.';
    status(s,state==='cancelled'?'The installation download was cancelled.':state==='paused'?'Download paused.':state==='cancelling'?'Cancelling the installation download…':job.error && typeof job.error==='string'?job.error:job.message || (success?'The installation files were updated.':job.phase || state),state==='failed');paintJobControls(s,job,terminal);
    var old=s.pane.querySelector('[data-iw-job-outcome]');if(old)old.remove();var outcome=node(s,'div');outcome.dataset.iwJobOutcome='';
    if(Number.isInteger(job.total) && job.total>0 && !terminal){var bar=node(s,'progress',null,'iw-job-progress',outcome);bar.dataset.iwJobProgress='';bar.max=job.total;bar.value=Math.max(0,Math.min(job.total,Number(job.completed) || 0));bar.setAttribute('aria-label','Verified downloads');node(s,'p',bar.value+' of '+bar.max+' files verified'+(job.phase==='committing'?'. Applying changes…':''),'__hx-note',outcome);}
    if(success && job.result && job.result.summary){var summary=job.result.summary;[['installed','Installed'],['reused','Reused existing'],['enabled','Enabled'],['removed','Replaced or removed']].forEach(function(pair){var items=summary[pair[0]] || [];if(!items.length)return;node(s,'h4',pair[1]+' ('+items.length+')',null,outcome);var list=node(s,'ul',null,null,outcome);items.forEach(function(item){node(s,'li',reviewName(s,{files:[]},item),null,list);});});}
    ((job.result && job.result.warnings) || []).forEach(function(w){node(s,'p',w.message || String(w),'__hx-note',outcome);});
    if(terminal){if(job.status==='failed')failureDetails(s,{details:job.details});button(s,['mods','resourcepacks','shaderpacks'].includes(s.category)?'Installed files':'Back to '+categories.find(function(c){return c[0]===s.category;})[1],function(){['mods','resourcepacks','shaderpacks'].includes(s.category)?content(s):tools(s);},outcome,false,'data-iw-job-installed');}
  }
  async function poll(s,id) {
    clearTimeout(s.poll);if(!live(s))return;var state=s.jobPoll;if(!state || state.id!==id)state=s.jobPoll={id:id,kind:jobs[s.id] && jobs[s.id].kind || s.category,inFlight:false,terminal:false,generation:0};if(state.inFlight || state.terminal || state.controlPending)return;state.inFlight=true;var generation=state.generation;
    try {var data=await request(s,'/job?jobId='+encodeURIComponent(id));if(!live(s) || s.jobPoll!==state || state.generation!==generation)return;var job=ownedJob(data,id,state.kind);rememberJob(s,state,job);
      if(state.terminal){s.committed=true;refreshFooter(s);return;}
      s.poll=setTimeout(function(){poll(s,id);},700);
    }catch(e){if(live(s) && s.jobPoll===state && state.generation===generation){if(s.pane.dataset.iwJobId===id)fail(s,e);s.poll=setTimeout(function(){poll(s,id);},1800);}}finally{state.inFlight=false;if(live(s) && s.jobPoll===state && state.generation!==generation && !state.controlPending && !state.terminal){clearTimeout(s.poll);s.poll=setTimeout(function(){poll(s,id);},700);}}
  }
  function showCategory(s,category) {s.category=category;s.pane.setAttribute('aria-labelledby','iw-tab-'+category);s.screen.el.querySelectorAll('[data-category]').forEach(function(b){b.setAttribute('aria-selected',String(b.dataset.category===category));b.tabIndex=b.dataset.category===category?0:-1;});refreshFooter(s);}
  function select(s,category) {
    if(!live(s) || !s.instance)return;showCategory(s,category);
    if(category==='general')general(s);else if(['mods','resourcepacks','shaderpacks'].includes(category))content(s);else tools(s);
  }
  async function tools(s) {
    if(gateDraft(s,function(){tools(s);}))return;
    clear(s);heading(s,categories.find(function(c){return c[0]===s.category;})[1]);status(s,'Loading installation data…');var epoch=s.epoch,kind=s.category,key=toolDraftKey(s,kind),generation=(s.toolGenerations || {})[key] || 0;
    try{var data=await request(s,'/tools?kind='+encodeURIComponent(kind));if(!live(s)||s.epoch!==epoch)return;if(((s.toolGenerations || {})[key] || 0)!==generation){tools(s);return;}refreshInstanceState(s,data);s.toolsRevision=data.revision;s.toolsContext={kind:kind,revision:data.revision};clear(s);heading(s,categories.find(function(c){return c[0]===kind;})[1]);if(data.message)node(s,'p',data.message,'__hx-note');renderTools(s,kind,data);}catch(e){readRecovery(s,e,epoch,function(){tools(s);},function(){select(s,'general');});}
  }
  function refreshTool(s,kind){if(kind==='datapacks')worldDatapacks(s,s.datapackWorld);else tools(s);}
  function toolAction(s,kind,action,extra,after,onAcknowledged) {
    var context=s.toolsContext && s.toolsContext.kind===kind?s.toolsContext:{revision:s.toolsRevision};var payload=Object.assign({kind:kind,action:action,worldId:context.worldId,revision:context.revision || (s.inventory ? s.inventory.revision:undefined)},extra || {}),epoch=s.epoch,key=toolDraftKey(s,kind);
    if(s.busy)return;s.busy=true;refreshFooter(s);status(s,'Working…');
    return request(s,'/tools',payload).then(async function(data){if(data.jobId)jobs[s.id]={id:data.jobId,status:'queued'};if(!data.cancelled && ['save','delete','rename','copy','import','restore','activate'].includes(action)){s.toolGenerations=s.toolGenerations || Object.create(null);s.toolGenerations[key]=(s.toolGenerations[key] || 0)+1;}if(!data.cancelled && onAcknowledged)onAcknowledged(data,s.epoch===epoch);if(!live(s)||s.epoch!==epoch)return;if(!data.cancelled)s.committed=true;if(data.jobId){progress(s,data.jobId);return;}if(action==='stop'){var instance=await request(s,'');if(!live(s))return;s.instance=instance;if(!changed(s)){s.base=values(instance);s.draft=copy(s.base);}if(s.epoch===epoch)refreshTool(s,kind);return;}if(!data.cancelled && after){after(data);return;}status(s,data.message || 'The launcher completed '+action+'.');if(!data.cancelled && action==='activate')return content(s);if(!data.cancelled && ['save','delete','rename','copy','import','restore','backup'].includes(action))refreshTool(s,kind);}).catch(function(e){fail(s,e,epoch);}).finally(function(){s.busy=false;refreshFooter(s);});
  }
  function paintDiagnosis(s,data,region){
    var diagnosis=data.diagnosis || {},fingerprint=JSON.stringify([diagnosis,data.sourceTruncated,data.diagnosisEnabled]);if(region.iwFingerprint===fingerprint)return;region.iwFingerprint=fingerprint;region.textContent='';
    if(data.diagnosisEnabled===false){node(s,'h4','Local crash explanations are off',null,region);node(s,'p','Enable Show local crash explanations in Settings to analyze these logs. You can still read and export them below.','__hx-note',region);return;}
    node(s,'h4','Session explanation',null,region);var states={'no-logs':'No logs available',unknown:'Session result unknown','issues-detected':'Logged issues detected',running:'Minecraft is running',successful:'Launch exited successfully',failed:'Launch failed'},scope=diagnosis.scope;
    node(s,'strong',scope==='previous'?'Previous session logs':scope==='current'?'Current session logs':'Session attribution unknown',null,region).dataset.iwLogScope='';
    if(scope==='previous')node(s,'p','This evidence belongs to a previous session and does not describe the result of the newer launch.','__hx-note',region);
    if(diagnosis.state)node(s,'p',states[diagnosis.state] || 'Session result unknown','__hx-note',region).dataset.iwLogState='';
    var summary=node(s,'p',diagnosis.summary || 'Inspect the local logs below. A session result has not been established.','__hx-note',region);summary.dataset.iwLogSummary='';summary.setAttribute('role','status');
    if(diagnosis.inputTruncated || data.sourceTruncated)node(s,'p','Only a bounded log tail was analyzed; earlier evidence may be missing.','__hx-note',region);
    if(diagnosis.evidenceTruncated)node(s,'p','Some evidence excerpts are shortened or omitted. Inspect the visible logs for surrounding lines.','__hx-note',region);
    (Array.isArray(diagnosis.findings)?diagnosis.findings:[]).slice(0,6).forEach(function(finding){
      var card=node(s,'article',null,'iw-log-finding',region);card.dataset.iwLogFinding=finding.id || '';node(s,'h4',finding.title || 'Logged issue',null,card);node(s,'p',finding.confidence==='reported'?'Reported in logs · A logged error does not by itself establish the cause of a crash.':'Likely cause · This interpretation is uncertain.','__hx-note',card);if(finding.explanation)node(s,'p',finding.explanation,null,card);
      if(Array.isArray(finding.suggestions) && finding.suggestions.length){node(s,'strong','What to check',null,card);var suggestions=node(s,'ul',null,null,card);finding.suggestions.forEach(function(suggestion){node(s,'li',suggestion,null,suggestions);});}
      if(Array.isArray(finding.evidence) && finding.evidence.length){node(s,'strong','Exact log evidence',null,card);finding.evidence.slice(0,4).forEach(function(evidence){node(s,'p',Number.isInteger(evidence.line)?'Line '+evidence.line:'Log excerpt','__hx-note',card);node(s,'pre',evidence.text || '', 'iw-log-evidence',card).dataset.iwLogEvidence='';if(evidence.truncated)node(s,'p','This excerpt is shortened.','__hx-note',card);});}
    });
  }
  function renderTools(s,kind,data) {
    var caps=data.capabilities || [],blocked=mutationReason(s),a=toolbar(s);
    function allowed(action,fn,parent,mutation){var b=button(s,action[0].toUpperCase()+action.slice(1),fn,parent || a);b.disabled=!caps.includes(action) || (mutation && !!blocked);return b;}
    if(kind==='notes') {
      var noteDraft=toolDraft(s,kind,data.text || '',data.revision),noteKey=toolDraftKey(s,kind);var label=node(s,'label','Installation notes');label.htmlFor='iw-notes';var notes=node(s,'textarea');notes.id='iw-notes';notes.value=noteDraft.value;
      notes.addEventListener('input',function(){noteDraft.value=notes.value;discardNote.disabled=s.busy || !toolDraftChanged(noteDraft);refreshFooter(s);});allowed('save',function(){var submitted=notes.value;noteDraft.value=submitted;toolAction(s,kind,'save',{text:submitted,revision:noteDraft.revision},function(){status(s,notes.value===submitted?'Notes saved.':'Notes saved. Your newer note changes are still unsaved.');},function(saved){noteDraft.base=submitted;noteDraft.revision=saved.revision || noteDraft.revision;discardNote.disabled=!toolDraftChanged(noteDraft);});},a,false);
      var discardNote=button(s,'Discard note changes',function(){if(s.busy)return;delete s.toolDrafts[noteKey];tools(s);},a);discardNote.disabled=s.busy || !toolDraftChanged(noteDraft);refreshFooter(s);return;
    }
    if(kind==='logs') {
      var diagnosis=node(s,'section',null,'iw-diagnosis');diagnosis.dataset.iwDiagnosis='';diagnosis.setAttribute('aria-label','Local log explanation');var source=node(s,'p','','__hx-note');source.dataset.iwLogSource='';var pre=node(s,'pre','','iw-logs scrollbars-default');pre.tabIndex=0;pre.setAttribute('aria-label','Visible installation logs');
      s.pane.insertAdjacentHTML('beforeend',s.ui.field('iw-log-search','Find in logs','Search log entries',s.logSearch || ''));var search=s.pane.querySelector('#iw-log-search');function draw(){var text=(data.text || '').split('\n').filter(function(l){return l.toLowerCase().includes(search.value.toLowerCase());}).join('\n') || (search.value?'No matching log entries.':'No log entries are available.');if(pre.textContent!==text){var scroll=pre.scrollTop;pre.textContent=text;pre.scrollTop=scroll;}}search.addEventListener('input',function(){s.logSearch=search.value;draw();});
      var exportLogs=allowed('export',function(){toolAction(s,kind,'export');}),stop=allowed('stop',function(){toolAction(s,kind,'stop');}),refresh=button(s,'Refresh',function(){refreshLogs();},a,false,'data-iw-log-refresh');
      if(navigator.clipboard && typeof navigator.clipboard.writeText==='function')button(s,'Copy visible logs',async function(){try{await navigator.clipboard.writeText(pre.textContent);status(s,'Visible logs copied.');}catch(e){fail(s,e);}},a);
      function paintLogs(){draw();paintDiagnosis(s,data,diagnosis);source.textContent='Source: '+(data.source || 'No selected log file');source.hidden=!data.source;stop.disabled=!(data.capabilities || []).includes('stop') || !data.running;exportLogs.disabled=!(data.capabilities || []).includes('export');}
      var epoch=s.epoch,pending=false;async function refreshLogs(){clearTimeout(s.logPoll);if(pending || !live(s)||s.epoch!==epoch)return;pending=true;refresh.disabled=true;try{var next=await request(s,'/tools?kind=logs');if(!live(s)||s.epoch!==epoch)return;refreshInstanceState(s,next);data=next;paintLogs();}catch(e){fail(s,e,epoch);}finally{pending=false;if(live(s)&&s.epoch===epoch){refresh.disabled=false;s.logPoll=setTimeout(refreshLogs,1000);}}}paintLogs();s.logPoll=setTimeout(refreshLogs,1000);return;
    }
    if(kind==='servers') {
      var serverDraft=toolDraft(s,kind,data.items || [],data.revision),serverKey=toolDraftKey(s,kind),servers=serverDraft.value,list=node(s,'div');
      function reflectServers(){if(discardServers)discardServers.disabled=s.busy || serverDraft.pending || !toolDraftChanged(serverDraft);refreshFooter(s);}
      function draw(focusIndex,scroll){list.textContent='';servers.forEach(function(server,index){var group=node(s,'div',null,'__hx-row',list);group.insertAdjacentHTML('beforeend',s.ui.field('iw-server-name-'+index,'Server name','',server.name)+s.ui.field('iw-server-address-'+index,'Address','localhost',server.address));group.querySelectorAll('input').forEach(function(input,i){input.disabled=!!blocked || serverDraft.pending;input.addEventListener('input',function(){server[i?'address':'name']=input.value;input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');var notice=s.pane.querySelector('[data-iw-status]');if(notice && notice.id==='iw-server-error')notice.textContent='';reflectServers();});});node(s,'p','Resource policy: '+(server.resourcePolicy || 'prompt'),'__hx-note',group);var actions=node(s,'div',null,'iw-toolbar',group);var remove=button(s,'Remove server',function(){var scroll=s.pane.scrollTop;servers.splice(index,1);draw(Math.min(index,servers.length-1),scroll);reflectServers();},actions);remove.setAttribute('aria-label','Remove '+(server.name || 'server '+(index+1)));remove.disabled=!!blocked || serverDraft.pending;});if(focusIndex!==undefined){var target=list.querySelector('#iw-server-name-'+focusIndex) || add;if(target)target.focus({preventScroll:true});if(scroll!==undefined)s.pane.scrollTop=scroll;else if(target)target.scrollIntoView({block:'nearest'});}}
      draw();var add=button(s,'Add server',function(){servers.push({name:'',address:'',resourcePolicy:'prompt'});draw(servers.length-1);reflectServers();},a);add.disabled=!!blocked || serverDraft.pending;var saveServers=allowed('save',function(){if(s.busy)return;var invalidIndex=servers.findIndex(function(v){return !v.name.trim() || !v.address.trim() || /\s/.test(v.address);});if(invalidIndex>=0){var server=servers[invalidIndex],field=list.querySelector('#iw-server-'+(!server.name.trim()?'name':'address')+'-'+invalidIndex);status(s,!server.name.trim()?'Enter a name for this server.':!server.address.trim()?'Enter an address for this server.':'Server addresses cannot contain spaces.',true);var notice=s.pane.querySelector('[data-iw-status]');notice.id='iw-server-error';field.setAttribute('aria-invalid','true');field.setAttribute('aria-describedby',notice.id);field.focus();return;}var controls=Array.from(list.querySelectorAll('input,button')).concat([add,saveServers,discardServers]);controls.forEach(function(b){b.disabled=true;});serverDraft.pending=true;toolAction(s,kind,'save',{items:servers,revision:serverDraft.revision},null,function(saved,sameView){delete s.toolDrafts[serverKey];if(live(s) && !sameView && s.category==='servers')refreshTool(s,kind);}).finally(function(){serverDraft.pending=false;if(!live(s)||!list.isConnected)return;controls.forEach(function(b){b.disabled=!!blocked;});reflectServers();});},a,true);saveServers.disabled=saveServers.disabled || serverDraft.pending;
      var discardServers=button(s,'Discard server changes',function(){if(s.busy)return;delete s.toolDrafts[serverKey];tools(s);},a);reflectServers();if(toolDraftChanged(serverDraft) && serverDraft.revision!==data.revision)node(s,'p','The saved server list changed while you were editing. Your draft is preserved; discard it to load the latest list.','__hx-note');if(blocked)node(s,'p',blocked,'__hx-note');return;
    }
    if(kind==='advanced') {
      var profile=s.instance.profile || {},context=s.instance.context || {};node(s,'p','Minecraft: '+context.minecraft+' · '+(context.loader || 'vanilla')+(context.loaderVersion?' '+context.loaderVersion:''),'__hx-note');
      node(s,'p','Directory: '+(s.instance.ownership.root || profile.gameDir || 'Launcher default'),'__hx-note');
      node(s,'p',profile.javaDir?'Java executable: '+profile.javaDir:'Java executable inherits the bundled runtime.','__hx-note');node(s,'p',profile.javaArgs?'JVM arguments override: '+profile.javaArgs:'JVM arguments inherit launcher defaults.','__hx-note');
      ['reveal','backup','export','repair'].filter(function(action){return caps.includes(action);}).forEach(function(action){allowed(action,function(){if(action==='repair')toolReview(s,kind,action);else toolAction(s,kind,action);},a,action==='repair' || action==='backup');});
      if(data.contentRecoveryPath && window.MCLauncher && typeof window.MCLauncher.openDirectory==='function'){var recoveryPending=false,recovery=button(s,'Open content recovery folder',async function(){if(recoveryPending)return;recoveryPending=true;recovery.disabled=true;var epoch=s.epoch;try{var opened=await window.MCLauncher.openDirectory(data.contentRecoveryPath);if(!live(s)||s.epoch!==epoch)return;if(opened===false)throw new Error('The launcher could not open the content recovery folder.');status(s,'Content recovery folder opened.');}catch(e){fail(s,e,epoch);}finally{recoveryPending=false;if(recovery.isConnected)recovery.disabled=false;}},a,false,'data-iw-content-recovery');}
      if(!caps.includes('repair'))node(s,'p','Loader repair is unavailable for this installation’s current loader and host capability.','__hx-note');
      heading(s,'Recoverable backups');var history=node(s,'ul',null,'iw-list');if(!(data.backups || []).length)node(s,'p','No recoverable backups have been recorded for this installation.','__hx-note');
      (data.backups || []).forEach(function(backup){var li=node(s,'li',null,'iw-fallback-row',history);node(s,'span',backup.name || backup.id,'iw-row-copy',li);var restore=button(s,'Review restore',function(){toolReview(s,'advanced','restore',backup);},li,false,'data-iw-restore-review');restore.disabled=!caps.includes('restore') || !!blocked;});
      button(s,'Java and JVM settings',function(){select(s,'general');s.pane.querySelector('#iw-javaDir').focus();},a);if(blocked)node(s,'p',blocked,'__hx-note');return;
    }
    if(caps.includes('import')){var imp=button(s,kind==='worlds'?'Import local world':kind==='screenshots'?'Import image':'Import datapack',function(){toolImport(s,kind);},a,false,'data-iw-tool-import');imp.disabled=!!blocked;}
    var list=node(s,'ul',null,'iw-list'),items=data.items || [];
    if(!items.length)node(s,'p','No '+kind+' were found in this installation.','__hx-note');
    items.forEach(function(item){row(s,list,item.id,item.name || item.id,item.version || item.path || '','Details',function(){clear(s);heading(s,item.name || item.id);if(kind==='screenshots' && item.url){var img=node(s,'img',null,'iw-image');img.src=item.url;img.alt=item.name || 'Screenshot';}node(s,'p',item.path || '','__hx-note');var actions=toolbar(s);button(s,'Back',function(){refreshTool(s,kind);},actions);caps.forEach(function(action){if(['save','import','restore'].includes(action))return;var mut=['rename','delete','copy'].includes(action);allowed(action,function(){if(['rename','delete'].includes(action))toolReview(s,kind,action,item);else if(action==='datapacks')worldDatapacks(s,item);else toolAction(s,kind,action,{id:item.id});},actions,mut);});if(blocked)node(s,'p',blocked,'__hx-note');});});
    if(kind==='screenshots')list.querySelectorAll('[data-content-id]').forEach(function(li){var item=items.find(function(i){return i.id===li.dataset.contentId;});if(!item || !item.url)return;var url=new URL(item.url,location.href);if(url.origin!==location.origin)return;var img=li.querySelector('img');if(!img){img=node(s,'img',null,null,li);li.insertBefore(img,li.firstChild);}img.src=url.href;img.alt=item.name || 'Screenshot';img.removeAttribute('aria-hidden');img.style.cssText='width:48px;height:48px;object-fit:contain';});
  }
  function toolImport(s,kind){clear(s);heading(s,kind==='worlds'?'Import local world':kind==='screenshots'?'Import image':'Import world datapack');node(s,'p',kind==='worlds'?'Choose a local ZIP containing one Minecraft world. The launcher validates it and copies it into this installation.':kind==='screenshots'?'Choose a local image to copy into this installation’s screenshots.':'Choose a local datapack ZIP. It is copied into the selected world; Minecraft controls its enabled state.','__hx-note');var a=toolbar(s);button(s,'Back',function(){refreshTool(s,kind);},a);button(s,'Choose file and import',function(){toolAction(s,kind,'import');},a,true,'data-iw-confirm-import');}
  function toolReview(s,kind,action,item) {
    clear(s);heading(s,action[0].toUpperCase()+action.slice(1)+(item?' '+(item.name || item.id):''));node(s,'p',action==='delete'?'The launcher keeps recoverable history for protected deletion.':action==='restore'?'This backup restores missing files without overwriting them. Existing files are preserved.':'This command changes installation files.','__hx-note');if(action==='rename')s.pane.insertAdjacentHTML('beforeend',s.ui.field('iw-tool-name','Name','',item.name));var a=toolbar(s);button(s,'Back',function(){refreshTool(s,kind);},a);button(s,action==='restore'?'Restore missing files':action[0].toUpperCase()+action.slice(1),function(){var extra=item?{id:item.id}:{};if(action==='rename'){extra.name=s.pane.querySelector('#iw-tool-name').value.trim();if(!extra.name){status(s,'Enter a name.',true);return;}}toolAction(s,kind,action,extra);},a,true,action==='restore'?'data-iw-restore':undefined);
  }
  async function worldDatapacks(s,item) {
    clear(s);heading(s,'Datapacks — '+item.name);var epoch=s.epoch;s.datapackWorld=item;
    try{var data=await request(s,'/tools?kind=datapacks&id='+encodeURIComponent(item.id));if(!live(s)||s.epoch!==epoch)return;refreshInstanceState(s,data);s.toolsContext={kind:'datapacks',worldId:data.worldId || item.id,revision:data.revision};s.toolsRevision=data.revision;if(data.message)node(s,'p',data.message,'__hx-note');button(s,'Back to worlds',function(){tools(s);},toolbar(s));renderTools(s,'datapacks',data);}catch(e){readRecovery(s,e,epoch,function(){worldDatapacks(s,item);},function(){tools(s);});}
  }
  function open(id,opener) {
    var ui=window.__launcherUI;if(!ui || !id)return;
    dispose(current);if(opener && opener.isConnected)opener.focus();style();var shell=ui.screen('Edit installation',1120),s={id:String(id),ui:ui,screen:shell,opener:opener,sequence:++sequence,requests:new Set(),epoch:0,closed:false,busy:false,category:'general',instance:null,inventory:null,inventoryScan:0,inventoryError:null,base:null,draft:null};current=s;shell.el.dataset.installationId=s.id;
    var rail=node(s,'div',null,'iw-rail scrollbars-default',shell.body);rail.setAttribute('role','tablist');rail.setAttribute('aria-label','Installation categories');rail.setAttribute('aria-orientation','vertical');
    s.pane=node(s,'section',null,'iw-pane scrollbars-default',shell.body);s.pane.dataset.iwPane='';s.pane.id='iw-pane';s.pane.setAttribute('role','tabpanel');s.pane.setAttribute('aria-labelledby','iw-tab-general');
    categories.forEach(function(c,index){var b=node(s,'button',c[1],'iw-category',rail);b.type='button';b.dataset.category=c[0];b.id='iw-tab-'+c[0];b.setAttribute('role','tab');b.setAttribute('aria-controls','iw-pane');b.setAttribute('aria-selected',String(index===0));b.tabIndex=index===0?0:-1;b.addEventListener('click',function(){select(s,c[0]);});});
    rail.addEventListener('keydown',function(e){var controls=Array.from(rail.querySelectorAll('button')),index=controls.indexOf(document.activeElement);if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;e.preventDefault();if(e.key==='Home')index=0;else if(e.key==='End')index=controls.length-1;else index=(index+(e.key==='ArrowDown'?1:-1)+controls.length)%controls.length;controls[index].focus();});
    s.footerNote=node(s,'p','','iw-footer-note',shell.el.querySelector('.__hx-foot'));s.footerNote.setAttribute('role','status');s.closeButton=shell.button('Cancel',false,function(){requestClose(s);});s.closeButton.dataset.iwClose='';s.saveButton=shell.button('Save',true,function(){save(s);});s.saveButton.dataset.iwSave='';s.saveButton.disabled=true;
    shell.el.querySelector('.__hx-close').addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();requestClose(s);},true);
    // The primitive owns Escape, focus trapping and background inertness. Its
    // removal observer also invalidates requests closed through native chrome.
    s.observer=new MutationObserver(function(){if(!shell.el.isConnected)dispose(s);});s.observer.observe(document.body,{childList:true});
    s.escapeMenu=function(e){if(e.key!=='Escape'||!live(s))return;var menu=s.pane.querySelector('.iw-options:not([hidden]),.iw-icon-menu:not([hidden])');if(menu){e.preventDefault();e.stopImmediatePropagation();menu.hidden=true;var trigger=menu.classList.contains('iw-icon-menu')?s.pane.querySelector('#iw-icon'):menu.parentElement.querySelector('.iw-trigger');trigger.setAttribute('aria-expanded','false');trigger.focus();return;}if(s.closeNotice && s.closeNotice.isConnected){e.preventDefault();e.stopImmediatePropagation();s.closeNotice.querySelector('[data-iw-keep-editing]').click();return;}if(unsavedCategories(s).length){e.preventDefault();e.stopImmediatePropagation();requestClose(s);}};window.addEventListener('keydown',s.escapeMenu,true);
    var opening=false;function loadInstallation(){if(opening || !live(s))return;opening=true;clear(s);status(s,'Loading installation…');var epoch=s.epoch;request(s,'').then(function(data){if(!live(s)||s.epoch!==epoch)return;s.instance=data;s.base=values(data);s.draft=copy(s.base);var title=shell.el.querySelector('h2');title.textContent='Edit installation — '+installationName(data);title.style.cssText='max-width:calc(100% - 120px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis';if(data.latestJob && data.latestJob.id){jobs[s.id]=data.latestJob;s.committed=true;}general(s);if(jobs[s.id] && !['succeeded','complete','failed','cancelled','idle'].includes(jobs[s.id].status)){s.category='mods';shell.el.querySelectorAll('[data-category]').forEach(function(b){b.setAttribute('aria-selected',String(b.dataset.category==='mods'));b.tabIndex=b.dataset.category==='mods'?0:-1;});s.pane.setAttribute('aria-labelledby','iw-tab-mods');progress(s,jobs[s.id].id);}}).catch(function(e){readRecovery(s,e,epoch,loadInstallation);}).finally(function(){opening=false;});}loadInstallation();return shell;
  }
  window.__installationWorkspace={open:open};
})();
