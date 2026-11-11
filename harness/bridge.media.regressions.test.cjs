// Real Chromium parser/request checks, isolated from launcher and user state.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('article markup localizes media before Chromium starts a remote request', () => {
    process.stdout.write(execFileSync(process.execPath,
      [path.join(__dirname,'../app/node_modules/electron/cli.js'), __filename],
      {encoding:'utf8',timeout:60000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const http = require('node:http');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(),'launcher-markup-'));
  app.setPath('userData',path.join(temporary,'electron'));
  app.disableHardwareAcceleration();
  let win, host;
  const report = {checks:[],remoteRequests:[],consoleErrors:[]};
  const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
  const run = code => win.webContents.executeJavaScript(code);
  app.on('window-all-closed',()=>{});
  app.whenReady().then(async()=>{
    try {
      const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jr1kAAAAASUVORK5CYII=','base64');
      host=http.createServer((req,res)=>{
        if(req.url.endsWith('.png')){res.writeHead(200,{'Content-Type':'image/png'});res.end(png);}
        else if(req.url.endsWith('.css')){res.writeHead(200,{'Content-Type':'text/css'});res.end('');}
        else {res.writeHead(200,{'Content-Type':'text/html'});res.end('<main id="article"></main>');}
      });
      await new Promise(resolve=>host.listen(0,'127.0.0.1',resolve));
      const origin='http://127.0.0.1:'+host.address().port;
      win=new BrowserWindow({show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false}});
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        const external=!details.url.startsWith(origin+'/') && !details.url.startsWith('data:');
        if(external)report.remoteRequests.push(details.url);
        callback({cancel:external});
      });
      win.webContents.on('console-message',event=>{if(event.level==='error'||event.level===3)report.consoleErrors.push(event.message);});
      await win.loadURL(origin+'/');
      const source=fs.readFileSync(path.join(__dirname,'bridge.js'),'utf8');
      const localize=source.slice(source.indexOf('  const isLocalUrl ='),source.indexOf('  // Some hosts'));
      const media=source.slice(source.indexOf('  const localizeSrcset ='),source.indexOf('  // --- sidebar scope'));
      await run(`(() => {const MIRRORED_ROOTS=[['https://launchercontent.mojang.com','']];const NETLOG=[];${localize}\n${media}\n})();void 0`);
      for(const mode of ['innerHTML','outerHTML','insertAdjacentHTML']) {
        const before=report.remoteRequests.length;
        await run(`(() => {const root=document.querySelector('#article');const html='<section data-markup-case="${mode}"><p>Captured article</p><img src=https://launchercontent.mojang.com/v2/images/first.png><picture><source srcset="https://launchercontent.mojang.com/v2/images/one.png 1x, https://launchercontent.mojang.com/v2/images/two.png 2x"><img src="//launchercontent.mojang.com/v2/images/fallback.png"></picture><a href="https://www.minecraft.net/article/example">Read more</a></section>';if('${mode}'==='innerHTML')root.innerHTML=html;else if('${mode}'==='outerHTML'){root.innerHTML='<div id="replace"></div>';document.querySelector('#replace').outerHTML=html;}else{root.replaceChildren();root.insertAdjacentHTML('beforeend',html);}})();void 0`);
        for(let i=0;i<50;i++){if(await run(`Array.from(document.querySelectorAll('#article img')).every(image=>image.complete)`))break;await pause(20);}
        await pause(100);
        const result=await run(`(() => {const root=document.querySelector('#article');return {text:root.querySelector('p').textContent,src:root.querySelector('img').getAttribute('src'),srcset:root.querySelector('source').getAttribute('srcset'),images:Array.from(root.querySelectorAll('img')).map(image=>image.naturalWidth),href:root.querySelector('a').getAttribute('href')};})()`);
        assert.equal(report.remoteRequests.length,before,mode+' started a remote parser request');
        assert.equal(result.text,'Captured article');
        assert.equal(result.src,'/v2/images/first.png');
        assert.equal(result.srcset,'/v2/images/one.png 1x, /v2/images/two.png 2x');
        assert.deepEqual(result.images,[1,1]);
        assert.equal(result.href,'https://www.minecraft.net/article/example');
        report.checks.push(mode);
      }
      assert.deepEqual(report.consoleErrors,[]);
    } catch(error){report.error=String(error.stack||error);}
    if(win&&!win.isDestroyed())win.destroy();
    if(host)await new Promise(resolve=>host.close(resolve));
    process.stdout.write(JSON.stringify(report)+'\n');
    app.exit(report.error?1:0);
  });
}
