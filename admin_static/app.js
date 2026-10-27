let state=null,tab='products',saveDialog=null,refreshing=null,qrFlow=null;
const $=s=>document.querySelector(s);
const e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,4500)}
async function request(path,data){const epoch=authEpoch;const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify(data)});const body=await r.json();if(epoch!==authEpoch)throw Error('登录状态已变化，请重新操作');if(r.status===401){lockWorkspace();await initAuth();throw Error(body.error||'请先登录')}if(!r.ok)throw Error(body.error||'操作失败');return body}
async function act(name,data){const result=await request('/api/'+name,data);toast(result.message);await refresh(true)}
async function refresh(force=false){const epoch=authEpoch;if(refreshing===epoch)return;refreshing=epoch;try{const incoming=await request('/api/state');if(epoch!==authEpoch||!authInfo)return;if(incoming.user.username!==authInfo.username){lockWorkspace();await initAuth();return}const changed=JSON.stringify(incoming)!==JSON.stringify(state);const templateDirty=$('#delivery-template')&&$('#delivery-template').value!==state?.delivery_template;state=incoming;if(force||(changed&&!templateDirty&&!$('#dialog').open&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)))render()}catch(err){if(epoch===authEpoch)toast(err.message)}finally{if(refreshing===epoch)refreshing=null}}
const productName=id=>state.products.find(p=>p.item===id)?.name||('商品 '+id);
const COOKIE_COPY_SCRIPT='copy(document.cookie)';
function cookieGuide(){return `<div class="cookie-tools"><a class="secondary" href="https://www.goofish.com/" target="_blank" rel="noopener noreferrer">打开闲鱼网页版 ↗</a></div><div class="cookie-script"><h3>快捷方式：控制台复制 Cookie</h3><p>在已登录的闲鱼网页打开开发者工具，选择“控制台 / Console”，输入下面的脚本并回车，再回到本页粘贴 Cookie。</p><div class="cookie-script-line"><code>${e(COOKIE_COPY_SCRIPT)}</code><button type="button" class="secondary" data-action="copy-cookie-script">复制脚本</button></div><p class="hint">此脚本只复制当前网页允许脚本读取的 Cookie，不会自动发送。如果复制结果为空、缺少登录字段或保存后仍登录失败，请按下面步骤复制完整的网络请求 Cookie。HttpOnly Cookie 无法由此脚本读取。</p></div><details class="cookie-guide" open><summary>完整方式：从网络请求获取 Cookie</summary><ol>
<li>点击上方按钮，在闲鱼网页版登录自己的卖家账号。</li>
<li>打开浏览器开发者工具：Mac 按 <kbd>⌘ Option I</kbd>；Windows 按 <kbd>F12</kbd> 或 <kbd>Ctrl Shift I</kbd>。</li>
<li>选择 <strong>网络 / Network</strong>，再刷新闲鱼网页（Mac：<kbd>⌘ R</kbd>；Windows：<kbd>Ctrl R</kbd>）。</li>
<li>在网络面板的过滤框输入 <code>h5api.m.goofish.com</code>，点击一条请求。如果没有结果，先在闲鱼打开自己的商品详情，再查看请求。</li>
<li>选择 <strong>标头 / Headers</strong>，找到 <strong>请求标头 / Request Headers</strong> 中的 <strong>Cookie</strong>。复制其后的完整值，格式为 <code>名称=值; 名称=值; …</code>。可以右键 Cookie 行选择“复制值 / Copy value”；不要复制响应里的 <code>Set-Cookie</code>。</li>
<li>回到本页，先停止发货服务，将完整内容粘贴到下方输入框，点击“保存 Cookie”，再选择运行模式并启动。</li>
</ol><p class="hint">复制整段 Cookie，不只复制某个 Token。仅粘贴到当前后台，不用发到聊天或截图中。</p></details>`}
function render(){const names={products:['商品与卡密','管理商品、维护库存，让每一笔交付都有记录。'],orders:['发货记录','分别核对卡密发送结果与平台发货状态。'],users:['账号管理','为不同使用者创建独立的工作空间。'],settings:['运行设置','管理本机连接与发货服务，随时开始或暂停。']};$('#title').textContent=names[tab][0];$('#crumb').textContent=names[tab][0];$('#subtitle').textContent=names[tab][1];$('#account').textContent=state.configured?'闲鱼账号 · '+state.seller:'尚未配置闲鱼账号';document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));$('#top-action').innerHTML=tab==='products'?'<button class="primary" data-action="add-product">＋ 添加商品</button>':tab==='users'?'<button class="primary" data-auth="add-user">＋ 创建账号</button>':'';const live=state.running&&state.mode==='live';const mock=state.running&&state.mode==='mock';$('#service-banner').className='banner'+(live?' live':'');$('#service-banner').innerHTML=`<span><b>${state.running?(live?'真实发货运行中':mock?'聊天模拟运行中':'演练观察运行中'):'发货服务已停止'}</b> · ${state.running?(live?'核实买家付款后发卡，并确认无需物流发货':mock?'买家新发“已支付”后回复测试卡密；不使用真实库存、不修改订单':'只查询订单，不发送卡密、不修改订单'):'添加商品和卡密后，可前往运行设置启动'}</span><button class="text-button" data-tab="settings">运行设置 ↗</button>`;const available=state.inventory.reduce((n,r)=>n+r.available,0);const values=[['商品数量',state.products.length,'已添加到本机工作空间','▦'],['可用卡密',available,'已发送的卡密不会重新分配','◇'],['卡密已发送',state.deliveries.filter(r=>r.state==='sent').reduce((n,r)=>n+(r.quantity||1),0),'以消息接口成功回执为准','↗'],['平台已发货',state.shipments.filter(r=>r.state==='done').length,'已回查订单发货状态','✓']];$('#stats').innerHTML=values.map(v=>`<div class="stat"><div class="stat-label">${v[0]}<em>${v[3]}</em></div><strong>${v[1]}</strong><small>${v[2]}</small></div>`).join('');if(tab==='products')renderProducts();if(tab==='orders')renderOrders();if(tab==='settings')renderSettings();if(tab==='users')renderUsers();if(state.running&&state.mode==='external'){$('#service-banner').className='banner live';$('#service-banner').firstElementChild.textContent='检测到外部发货进程 · 模式未知，请在原终端停止后交给后台管理';if($('#mode'))$('#mode').innerHTML='<option>外部进程 · 模式未知</option>'}}
function renderProducts(){$('#view').innerHTML=`<div class="panel"><div class="panel-head"><h2>商品列表</h2><small>${state.products.length} 个商品 · 库存按商品独立管理</small></div>${state.products.length?`<div class="table-wrap"><table><thead><tr><th>商品</th><th>状态</th><th>可用 / 总卡密</th><th>操作</th></tr></thead><tbody>${state.products.map(p=>{const stock=state.inventory.find(s=>s.item===p.item)||{available:0,total:0};return `<tr><td><strong>${e(p.name)}</strong><small>ID ${e(p.item)}</small></td><td><span class="pill ${p.enabled?'green':''}">${p.enabled?'已启用':'已暂停'}</span></td><td>${stock.available}<span class="hint"> / ${stock.total}</span></td><td><div class="row-actions"><button class="text-button" data-action="import" data-item="${p.item}">导入卡密</button><button class="text-button" data-action="cards" data-item="${p.item}">管理库存</button><button class="text-button" data-action="edit-product" data-item="${p.item}">编辑</button></div></td></tr>`}).join('')}</tbody></table></div>`:'<div class="empty"><div class="empty-symbol">▦</div><h3>先添加你的第一个商品</h3><p>粘贴闲鱼商品链接，再导入对应的卡密库存。</p><button class="primary" data-action="add-product">＋ 添加商品</button></div>'}</div>`}
function renderOrders(){$('#view').innerHTML=`<div class="panel"><div class="panel-head"><h2>发货流水</h2><small>发送失败或回执不确定时不会重复抽卡</small></div>${state.deliveries.length?`<div class="table-wrap"><table><thead><tr><th>订单 / 商品</th><th>卡密发送</th><th>平台发货</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${state.deliveries.map(d=>{const shipping=state.shipments.find(s=>s.order_id===d.order_id);const names={sent:'消息已确认',sending:'发送中 · 需核对',uncertain:'结果不确定'};const sn={done:'已发货',pending:'待确认发货',blocked:'需人工核对'};return `<tr><td><strong>${e(d.order_id)}</strong><small>${e(productName(d.item))} · ${d.quantity||1} 张卡密</small></td><td><span class="pill ${d.state==='sent'?'green':'amber'}">${names[d.state]||e(d.state)}</span></td><td><span class="pill ${shipping?.state==='done'?'green':'amber'}">${shipping?sn[shipping.state]:'尚未进入发货队列'}</span></td><td>${new Date(d.updated*1000).toLocaleString('zh-CN')}</td><td>${shipping?.state==='blocked'?`<button class="text-button" data-action="retry" data-order="${e(d.order_id)}">重试平台发货</button>`:'—'}</td></tr>`}).join('')}</tbody></table></div>`:'<div class="empty"><div class="empty-symbol">⇄</div><h3>还没有真实发货记录</h3><p>真实卡密消息发送后会显示在这里，模拟记录不计入。</p></div>'}</div>`}
function renderSettings(){$('#view').innerHTML=`<div class="setting-grid"><div class="panel"><div class="panel-head"><h2>闲鱼连接</h2><span class="pill ${state.configured?'green':''}">${state.configured?'已保存 Cookie':'待配置'}</span></div><div class="setting-body">${cookieGuide()}<div class="cookie-tools"><button type="button" class="secondary" data-action="check-login">检测登录状态 / 验证后重新检测</button><button type="button" class="secondary" data-action="server-browser" ${state.running?'disabled':''}>服务器浏览器验证</button></div><label for="cookie">手动更新 Cookie</label><input id="cookie" type="password" autocomplete="off" placeholder="粘贴完整 Cookie，已保存的内容不会回显"><p class="hint">登录信息保存在服务器的当前账号工作空间中；更新前请先停止发货服务。</p><button class="secondary" data-action="cookie" ${state.running?'disabled':''}>保存 Cookie</button><details class="cookie-alternative"><summary>其他登录方式：扫码登录</summary><button class="primary" data-action="qr-login" ${state.running?'disabled':''}>扫码登录闲鱼</button><p class="hint">用手机闲鱼扫码并确认后，自动保存到当前后台账号。请先停止发货服务；登录后自行选择并启动运行模式。</p></details></div></div><div class="panel"><div class="panel-head"><h2>发货服务</h2><span class="pill ${state.running?'green':''}">${state.running?'运行中':'已停止'}</span></div><div class="setting-body"><label for="mode">运行模式</label><select id="mode" ${state.running?'disabled':''}><option value="observe" ${state.mode==='observe'?'selected':''}>演练观察 · 不发卡、不改订单</option><option value="mock" ${state.mode==='mock'?'selected':''}>聊天模拟 · 收到“已支付”回复测试卡密</option><option value="live" ${state.mode==='live'?'selected':''}>真实发货 · 付款后发卡并确认已发货</option></select><p class="hint">聊天模拟：启动后让其他账号新发“已支付”，按下方文案回复 MOCK-TEST 测试卡密，不消耗真实库存、不修改订单。发“已支付 2”可模拟一条消息发两张。演练观察不回复消息。真实模式按订单购买数量一次发送对应卡密，库存不足不发半单；切换模式前先停止服务。</p><button class="${state.running?'secondary danger':'primary'}" data-action="${state.running?'stop':'start'}">${state.running?'停止发货服务':'启动所选模式'}</button></div></div><div class="panel wide"><div class="panel-head"><h2>发货文案</h2><small>模拟与真实发货共用 · 当前账号独立配置</small></div><div class="setting-body"><label for="delivery-template">消息内容</label><textarea id="delivery-template" rows="9" maxlength="5000">${e(state.delivery_template)}</textarea><p class="hint">保留且仅保留一个 {card}，发送时会自动替换为本单卡密。请将 xxx、xxxx 换成你的真实教程和下载链接。保存后下一次发卡生效，无需重启。</p><button class="secondary" data-action="delivery-template">保存发货文案</button><label for="template-preview">消息预览（示例卡密，不会发送）</label><pre id="template-preview"></pre></div></div><div class="panel wide"><div class="panel-head"><h2>运行日志</h2><small>自动刷新 · 不显示 Cookie 或卡密正文</small></div><pre id="logs"></pre></div></div>`;$('#template-preview').textContent=state.delivery_template.replace('{card}','示例卡密-123456');$('#logs').textContent=state.logs.join('\n')||'服务尚未启动。'}
function dialog(title,body,save,label='保存'){$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=body;$('#dialog-save').textContent=label;$('#dialog-save').hidden=!save;saveDialog=save;$('#dialog').showModal()}
function productDialog(p){dialog(p?'编辑商品':'添加商品',`<label for="item">闲鱼商品链接或 ID</label><input id="item" value="${e(p?.item||'')}" ${p?'readonly':''} required placeholder="https://www.goofish.com/item?id=…"><label for="name">商品名称</label><input id="name" value="${e(p?.name||'')}" required maxlength="120" placeholder="例如：会员月卡"><div class="toggle"><input type="checkbox" id="enabled" ${!p||p.enabled?'checked':''}><label for="enabled">启用此商品的自动发货</label></div><p class="hint">暂停商品后不会分配新卡密，已发出的订单仍会完成平台发货确认。</p>`,async()=>act('product',{item:$('#item').value,name:$('#name').value,enabled:$('#enabled').checked}))}
async function cardsDialog(item){
 const rows=await request('/api/cards?item='+encodeURIComponent(item));
 dialog('管理库存 · '+productName(item),`<p class="hint">卡密内容完整显示。未分配的卡密可以修改、停用或删除；点击已分配卡密的“重新启用”会释放原分配并恢复为可分配库存（多卡订单整单释放）。列表最多显示最近500条；“全选”包含当前可操作库存。</p>
 <div class="inventory-toolbar"><label><input type="checkbox" id="cards-select-all" ${rows.length?'':'disabled'}> 全选未分配库存</label><span id="cards-selected-count">未选择</span><button type="button" class="secondary danger" data-action="delete-selected-cards" data-item="${item}" disabled>删除所选</button></div>
 <div class="card-list">${rows.length?rows.map(c=>{const allocated=Boolean(c.order_id);const locked=allocated;return `<div class="card-row"><label class="card-select"><input type="checkbox" class="card-checkbox" value="${c.id}" ${locked?'disabled':''}><span><code>#${c.id} · ${e(c.secret)}</code><small class="hint"> ${c.disabled?'已停用':allocated?'已分配':'可用'}</small></span></label><div class="row-actions"><button type="button" class="text-button" data-action="edit-card" data-id="${c.id}" data-item="${item}" ${locked?'disabled':''}>修改</button>${locked?`<button type="button" class="text-button" data-action="unlock-card" data-id="${c.id}" data-item="${item}">重新启用</button>`:`<button type="button" class="text-button" data-action="disable-card" data-id="${c.id}" data-item="${item}" data-disabled="${c.disabled?0:1}">${c.disabled?'恢复':'停用'}</button>`}<button type="button" class="text-button danger" data-action="delete-card" data-id="${c.id}" data-item="${item}" ${locked?'disabled':''}>删除</button></div></div>`}).join(''):'<div class="empty">暂无卡密，请先导入。</div>'}</div>`,null)
}
function updateCardSelection(allChanged=false){
 const all=$('#cards-select-all');if(!all)return;
 const boxes=[...document.querySelectorAll('.card-checkbox:not(:disabled)')];
 if(allChanged)boxes.forEach(box=>box.checked=all.checked);
 else{all.checked=false;all.indeterminate=boxes.some(box=>box.checked)}
 const count=boxes.filter(box=>box.checked).length;
 $('#cards-selected-count').textContent=all.checked?'已选择全部未分配库存':`已选择 ${count} 条`;
 document.querySelector('[data-action="delete-selected-cards"]').disabled=!all.checked&&!count;
}
async function deleteInventory(item,ids,all=false){
 const scope=all?'该商品全部未分配库存（包含未显示的记录）':`选中的 ${ids.length} 条卡密`;
 if(!window.confirm(`确定删除${scope}？\n已锁定的已分配卡密需要先重新启用。\n删除后从库存移除；重新导入相同卡密可以恢复。`))return;
 await act('delete-cards',{item,ids,all_unused:all});
 $('#dialog').close();await cardsDialog(item);
}
document.addEventListener('change',event=>{if(event.target.id==='cards-select-all')updateCardSelection(true);else if(event.target.classList.contains('card-checkbox'))updateCardSelection()});
document.addEventListener('click',async event=>{const b=event.target.closest('button');if(!b||b.disabled)return;try{if(b.dataset.tab){tab=b.dataset.tab;render();return}const a=b.dataset.action,p=state?.products.find(p=>p.item===b.dataset.item);if(a==='check-login'){b.disabled=true;try{const result=await request('/api/check-login',{});dialog('闲鱼登录检测',`<p>${e(result.message)}</p>${result.codes?.length?`<p class="hint">错误码：${e(result.codes.join(' / '))}</p>`:''}${result.verification_url?`<a class="primary" href="${e(result.verification_url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">打开闲鱼官方验证 ↗</a><p class="hint">请本人完成滑块、短信或手机确认。完成后关闭此窗口，重新检测。若仍要求验证，请重新保存完整 Cookie；验证可能绑定服务器环境。</p>`:''}`,null)}finally{b.disabled=false}return}if(a==='copy-cookie-script'){await navigator.clipboard.writeText(COOKIE_COPY_SCRIPT);toast('\u811a\u672c\u5df2\u590d\u5236\uff0c\u8bf7\u5728\u95f2\u9c7c\u7f51\u9875\u7684\u63a7\u5236\u53f0\u6267\u884c');return}if(a==='add-product')productDialog();if(a==='edit-product')productDialog(p);if(a==='import')dialog('导入卡密 · '+p.name,'<label for="batch">一行一个卡密</label><textarea id="batch" required placeholder="在此粘贴真实卡密，每行一条"></textarea><label for="file">或读取本地文本文件</label><input type="file" id="file" accept=".txt,.csv"><p class="hint">文件按纯文本逐行读取；请勿包含表头。重复卡密会自动跳过，不接受 MOCK 测试卡密。</p>',async()=>act('import',{item:p.item,text:$('#batch').value}),'导入库存');if(a==='cards')await cardsDialog(b.dataset.item);if(a==='edit-card'){const v=await request('/api/card?id='+b.dataset.id);$('#dialog').close();dialog('修改未使用卡密',`<label for="secret">卡密内容</label><input id="secret" required value="${e(v.secret)}"><p class="hint">修改后旧值会被替换，已分配的卡密不可修改。</p>`,async()=>act('edit-card',{id:Number(b.dataset.id),secret:$('#secret').value}))}if(a==='unlock-card'){await act('unlock-card',{id:Number(b.dataset.id)});$('#dialog').close();await cardsDialog(b.dataset.item)}if(a==='delete-card')await deleteInventory(b.dataset.item,[Number(b.dataset.id)]);if(a==='delete-selected-cards')await deleteInventory(b.dataset.item,[...document.querySelectorAll('.card-checkbox:checked')].map(box=>Number(box.value)),$('#cards-select-all').checked);if(a==='disable-card'){await act('edit-card',{id:Number(b.dataset.id),disabled:b.dataset.disabled==='1'});$('#dialog').close();await cardsDialog(b.dataset.item)}if(a==='retry')await act('retry-shipping',{order:b.dataset.order});if(a==='delivery-template')await act('delivery-template',{template:$('#delivery-template').value});if(a==='qr-login')await beginQrLogin();if(a==='cookie'){await act('cookie',{cookie:$('#cookie').value});$('#cookie').value=''}if(a==='start')await act('start',{mode:$('#mode').value});if(a==='stop')await act('stop',{});}catch(err){toast(err.message)}});
$('#dialog-form').addEventListener('submit',async ev=>{ev.preventDefault();if(!saveDialog)return;$('#dialog-save').disabled=true;try{await saveDialog();$('#dialog').close();if(authInfo)await refresh(true)}catch(err){toast(err.message)}finally{$('#dialog-save').disabled=false}});
$('#close-dialog').onclick=$('#cancel-dialog').onclick=()=>$('#dialog').close();document.addEventListener('change',async ev=>{if(ev.target.id==='file'&&ev.target.files[0])$('#batch').value=await ev.target.files[0].text()});initAuth();setInterval(()=>{if(authInfo)refresh()},5000);

document.addEventListener('input',event=>{if(event.target.id==='delivery-template')$('#template-preview').textContent=event.target.value.replace('{card}','示例卡密-123456')});

function forgetQrLogin(){if(qrFlow?.timer)clearTimeout(qrFlow.timer);qrFlow=null}
async function cancelQrLogin(){const flow=qrFlow;forgetQrLogin();if(flow&&flow.epoch===authEpoch&&authInfo){try{await request('/api/qr-login-cancel',{id:flow.id})}catch{}}}
async function beginQrLogin(){
 const epoch=authEpoch;
 const info=await request('/api/qr-login-start',{});
 if(epoch!==authEpoch)return;
 qrFlow={id:info.id,epoch,timer:null};
 dialog('扫码登录闲鱼','<p>打开手机闲鱼扫一扫，扫描下方二维码，并在手机上确认登录。</p><div class="qr-login-box"><img id="qr-login-image" alt="闲鱼登录二维码" hidden><p id="qr-login-message" role="status">正在获取二维码…</p></div><p class="hint">登录信息直接保存在服务器，不会在页面显示。二维码约5分钟失效；平台要求额外验证时，请本人完成验证后重试。</p>',null);
 await pollQrLogin();
}
async function pollQrLogin(){
 const flow=qrFlow;if(!flow||flow.epoch!==authEpoch)return;
 try{
  const info=await request('/api/qr-login-status',{id:flow.id});
  if(qrFlow!==flow||flow.epoch!==authEpoch||!$('#dialog').open)return;
  const messages={preparing:'正在获取二维码…',waiting:'请用手机闲鱼扫码',scanned:'已扫码，请在手机闲鱼确认登录',saved:'登录成功，已自动保存',expired:'二维码已过期，请关闭后重新扫码',cancelled:'登录已取消',verification_required:'平台要求额外验证，请在闲鱼官方网页完成验证后重试',error:'无法完成扫码登录，可能是平台验证或网络问题，请稍后重试；也可使用手动 Cookie'};
  const errorDetails={missing_account:'平台尚未返回账号登录凭证，请在手机确认登录后重新扫码；若要求验证，请先在闲鱼官方网页完成',missing_token:'平台未返回接口凭证，请稍后重新扫码',network_timeout:'连接闲鱼接口超时，请稍后重试',network_error:'连接闲鱼接口失败，请稍后重试',http_error:'闲鱼接口拒绝请求，请稍后重试',unknown_scan_status:'闲鱼返回了暂不支持的扫码状态，请重试',invalid_response:'闲鱼登录接口返回异常，请重试'};
  const stages={initializing:'初始化',login_page:'获取登录页',generate_qr:'生成二维码',poll_status:'查询扫码状态',collect_credentials:'获取登录凭证',refresh_token:'更新接口凭证'};
  $('#qr-login-message').textContent=info.status==='error'?((errorDetails[info.error_code]||messages.error)+'（阶段：'+(stages[info.stage]||'登录')+'；错误码：'+(info.error_code||'unknown')+'）'):(messages[info.status]||'正在确认登录…');
  const image=$('#qr-login-image');image.hidden=!info.image;
  if(info.image)image.src=info.image;else image.removeAttribute('src');
  if(info.status==='saved'){forgetQrLogin();$('#dialog').close();toast('闲鱼登录成功，已自动保存。请选择运行模式并启动服务');await refresh(true);return}
  if(['expired','cancelled','verification_required','error'].includes(info.status)){forgetQrLogin();return}
  flow.timer=setTimeout(pollQrLogin,2000);
 }catch(err){if(qrFlow===flow){forgetQrLogin();if($('#qr-login-message'))$('#qr-login-message').textContent=err.message}}
}
$('#dialog').addEventListener('close',()=>{void cancelQrLogin()});

let serverBrowser=null;
async function closeServerBrowser(){
 const flow=serverBrowser;serverBrowser=null;
 if(flow?.timer)clearTimeout(flow.timer);
 $('#dialog').classList.remove('browser-dialog');
 if(flow&&flow.epoch===authEpoch&&authInfo){try{await request('/api/browser-close',{id:flow.id})}catch{}}
}
async function beginServerBrowser(){
 const epoch=authEpoch;
 const result=await request('/api/browser-start',{});
 if(epoch!==authEpoch)return;
 serverBrowser={id:result.id,epoch,busy:false,timer:null,points:null};
 dialog('服务器浏览器 · 闲鱼人工验证',`<p class="hint">下面是服务器上的浏览器。可直接点击、拖动滑块；登录时优先使用扫码。有效期10分钟。完成后点击“保存登录并检测”。本窗口不会自动启动发货。</p><p id="browser-status" role="status">正在启动浏览器，请稍候…</p><img id="browser-screen" alt="服务器浏览器画面" draggable="false" hidden><div class="browser-controls"><input id="browser-text" type="password" autocomplete="off" maxlength="128" placeholder="如需输入，先点击画面中的输入框，再在此填写"><button type="button" class="secondary" data-browser="text">输入到画面</button><button type="button" class="secondary" data-browser="key" data-key="Control+A">全选</button><button type="button" class="secondary" data-browser="key" data-key="Backspace">退格</button><button type="button" class="secondary" data-browser="key" data-key="Enter">回车</button><button type="button" class="secondary" data-browser="wheel" data-delta="-500">向上滚动</button><button type="button" class="secondary" data-browser="wheel" data-delta="500">向下滚动</button><button type="button" class="secondary" data-browser="home">回到闲鱼</button><button type="button" class="secondary" data-browser="refresh">刷新页面</button><button type="button" class="primary" data-browser="save">保存登录并检测</button></div>`,null);
 $('#dialog').classList.add('browser-dialog');
 const screen=$('#browser-screen');
 const point=event=>{const r=screen.getBoundingClientRect();return [Math.max(0,Math.min(1099,Math.round((event.clientX-r.left)*1100/r.width))),Math.max(0,Math.min(779,Math.round((event.clientY-r.top)*780/r.height)))];};
 screen.onpointerdown=event=>{const f=serverBrowser;if(!f||f.busy)return;event.preventDefault();f.points=[point(event)];screen.setPointerCapture(event.pointerId);};
 screen.onpointermove=event=>{const f=serverBrowser;if(f?.points&&f.points.length<119)f.points.push(point(event));};
 screen.onpointerup=event=>{const f=serverBrowser;if(!f?.points)return;f.points.push(point(event));const points=f.points;f.points=null;void browserCommand('input',{kind:'pointer',points});};
 screen.onpointercancel=()=>{if(serverBrowser)serverBrowser.points=null;};
 await browserCommand('status');
}
function browserFrame(result){
 if(result.image&&$('#browser-screen')){$('#browser-screen').src=result.image;$('#browser-screen').hidden=false;}
 if($('#browser-status'))$('#browser-status').textContent=result.error||result.message||(result.status==='preparing'?'正在启动浏览器，请稍候…':'请在画面中完成闲鱼验证或登录，再保存并检测。');
}
async function browserCommand(action,data={}){
 const flow=serverBrowser;if(!flow||flow.busy||flow.points||flow.epoch!==authEpoch)return;
 if(flow.timer)clearTimeout(flow.timer);flow.busy=true;
 try{
  const result=await request('/api/browser-'+action,{id:flow.id,...data});
  if(serverBrowser!==flow||flow.epoch!==authEpoch)return;
  browserFrame(result);
  if(action==='save'&&result.status==='ok')toast('登录检测通过，可关闭验证窗口并启动发货服务');
 }catch(err){if(serverBrowser===flow&&$('#browser-status'))$('#browser-status').textContent=err.message;}
 finally{flow.busy=false;if(serverBrowser===flow&&$('#dialog').open)flow.timer=setTimeout(()=>browserCommand('status'),2000);}
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('button');if(!button||button.disabled)return;
 try{
  if(button.dataset.action==='server-browser'){button.disabled=true;try{await beginServerBrowser();}finally{button.disabled=false;}}
  if(button.dataset.browser){
   const action=button.dataset.browser;
   if(action==='save')await browserCommand('save');
   else if(action==='text'){const input=$('#browser-text');const text=input.value;input.value='';if(text)await browserCommand('input',{kind:'text',text});}
   else await browserCommand('input',{kind:action,key:button.dataset.key,delta:Number(button.dataset.delta)});
  }
 }catch(err){toast(err.message);}
});
$('#dialog').addEventListener('close',()=>{void closeServerBrowser();});
