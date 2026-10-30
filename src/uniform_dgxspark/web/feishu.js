const root = '/api/v1/system/feishu';
const actionHeaders = {'X-Uniform-Action':'feishu-notifications'};
const labels = {gpu_temperature:'GPU 温度',memory:'统一内存',disk:'磁盘占用',cpu:'CPU 使用率',gpu:'GPU 使用率',swap:'Swap 占用',offline:'节点离线'};
let api, escapeHtml, toast, status, hydrated=false, dirty=false, busy=false, loading=false;

export function initFeishuNotifications(apiFunction, escapeFunction, toastFunction) {
  api=apiFunction; escapeHtml=escapeFunction; toast=toastFunction;
  const form=document.querySelector('#feishu-form');
  form.addEventListener('input', event=>{
    dirty=true;
    if(['app_id','app_secret','redirect_uri'].includes(event.target.name)) form.elements.enabled.checked=false;
    syncButtons();
  });
  form.addEventListener('change',()=>{dirty=true;syncButtons();});
  form.addEventListener('submit',async event=>{
    event.preventDefault(); if(busy)return;
    const payload={app_id:form.elements.app_id.value.trim(),redirect_uri:form.elements.redirect_uri.value.trim(),
      enabled:form.elements.enabled.checked,notify_recovery:form.elements.notify_recovery.checked,
      include_remote_nodes:form.elements.include_remote_nodes.checked,
      rules:[...document.querySelectorAll('[data-feishu-rule]')].map(row=>({metric:row.dataset.feishuRule,
        enabled:row.querySelector('[data-rule-enabled]').checked,threshold:Number(row.querySelector('[data-rule-threshold]').value),
        recovery:Number(row.querySelector('[data-rule-recovery]').value),duration_seconds:Number(row.querySelector('[data-rule-duration]').value),
        cooldown_seconds:Number(row.querySelector('[data-rule-cooldown]').value)}))};
    if(form.elements.app_secret.value)payload.app_secret=form.elements.app_secret.value;
    await perform(async()=>{
      status=await api(root+'/config',{method:'PUT',headers:actionHeaders,body:JSON.stringify(payload)});
      dirty=false;renderStatus();toast('飞书配置已保存');
    });
    form.elements.app_secret.value='';
  });
  document.querySelector('#feishu-bind').addEventListener('click',async()=>{
    if(status?.recipient&&!confirm('重新绑定将更换告警接收人，之后需要重新发送测试消息。继续？'))return;
    const popup=window.open('about:blank','uniform-feishu-authorization','width=720,height=800');
    if(!popup){toast('请允许此平台打开授权窗口后再试',true);return;}
    await perform(async()=>{
      try{
        const result=await api(root+'/bind',{method:'POST',headers:actionHeaders});
        const url=new URL(result.authorization_url);
        if(url.origin!=='https://accounts.feishu.cn'||url.pathname!=='/oauth/v3/authorize')throw new Error('授权地址校验失败');
        popup.location.href=url.href;toast('请在飞书官方授权页扫码并确认，完成后返回此页面');
      }catch(error){popup.close();throw error;}
    });
  });
  document.querySelector('#feishu-test').addEventListener('click',async()=>{
    if(!confirm('给已绑定的飞书账号发送一条机器人测试消息？'))return;
    await perform(async()=>{status=await api(root+'/test',{method:'POST',headers:actionHeaders});renderStatus();toast('测试消息已发送，请检查机器人私聊');});
  });
  document.querySelector('#feishu-unbind').addEventListener('click',async()=>{
    if(!confirm('解绑接收人并关闭自动告警？历史通知记录保留。'))return;
    await perform(async()=>{status=await api(root+'/binding',{method:'DELETE',headers:actionHeaders});dirty=false;renderStatus();toast('已解绑并关闭告警');});
  });
}

async function perform(action) {
  if(busy)return;
  busy=true;syncButtons();document.querySelector('#feishu-error').textContent='';
  try{await action();}
  catch(error){document.querySelector('#feishu-error').textContent=error.message;toast(error.message,true);}
  finally{busy=false;syncButtons();await loadFeishuNotifications();}
}

function syncButtons() {
  const form=document.querySelector('#feishu-form');
  document.querySelector('#feishu-save').disabled=busy||!hydrated;
  document.querySelector('#feishu-bind').disabled=busy||dirty||!status?.configured||status?.config.enabled;
  document.querySelector('#feishu-test').disabled=busy||dirty||!status?.configured||!status?.recipient;
  document.querySelector('#feishu-unbind').disabled=busy||!status?.recipient;
  form.elements.enabled.disabled=busy||!status?.recipient?.verified_at;
}

function renderStatus() {
  const config=status.config, recipient=status.recipient;
  document.querySelector('#feishu-status').textContent=config.enabled?'告警已开启':!status.configured?'等待配置飞书应用':!recipient?'等待扫码绑定':!recipient.verified_at?'等待测试消息':'已就绪 · 告警关闭';
  document.querySelector('#feishu-recipient').textContent=recipient?`接收人：${recipient.name} · ${recipient.verified_at?'测试消息已通过':'尚未通过测试消息'} · ${status.last_check_at?'最近检查 '+new Date(status.last_check_at*1000).toLocaleTimeString():''}`:'尚未绑定接收人。先创建并配置应用，再打开官方授权页扫码绑定。';
  if(status.last_error)document.querySelector('#feishu-error').textContent=status.last_error;
  const diagnostic=status.last_binding;
  const diagnosticPanel=document.querySelector('#feishu-binding-diagnostic');
  diagnosticPanel.hidden=!diagnostic;
  if(diagnostic){
    const stages={authorization:'打开授权页',callback:'验证回调',user_token:'向飞书换取凭证',user_info:'读取绑定身份',complete:'完成绑定'};
    const results={pending:'等待完成',failed:'失败',success:'成功'};
    const reasons={expired:'授权过期',browser_mismatch:'授权窗口与浏览器不匹配',local_pkce_mismatch:'本地 PKCE 参数不一致',configuration_changed:'授权期间应用配置已改变',invalid_code:'未收到有效授权码',user_denied:'用户未完成授权',provider_failed:'飞书接口拒绝或网络失败',invalid_provider_response:'飞书响应格式无效',storage_failed:'本地绑定存储失败'};
    const checkLabels={authorization_parameters_valid:'发出的授权参数完整',state_valid:'授权状态有效',browser_nonce_valid:'属于当前浏览器',pkce_verifier_valid:'校验字符串格式有效',pkce_challenge_matches:'本地 PKCE 前后匹配',redirect_uri_matches:'回调配置未改变'};
    document.querySelector('#feishu-binding-summary').textContent=`最近绑定：${results[diagnostic.result]||'待检查'} · ${stages[diagnostic.stage]||'授权流程'}${diagnostic.error_code==null?'':` · 飞书错误码 ${diagnostic.error_code}`}`;
    const lines=[`诊断编号：${diagnostic.attempt_id}`,`授权方式：v3 / PKCE S256`,`更新时间：${new Date(diagnostic.checked_at*1000).toLocaleString()}`];
    if(diagnostic.reason)lines.push(`原因：${reasons[diagnostic.reason]||'请检查授权流程'}`);
    if(diagnostic.http_status!=null)lines.push(`飞书 HTTP 状态：${diagnostic.http_status}`);
    if(diagnostic.request_id)lines.push(`飞书请求编号：${diagnostic.request_id}`);
    for(const [key,value] of Object.entries(diagnostic.checks||{}))if(value!=null&&checkLabels[key])lines.push(`${checkLabels[key]}：${value?'通过':'未通过'}`);
    lines.push('不记录密钥、授权码、校验字符串或完整回调链接。');
    document.querySelector('#feishu-binding-details').textContent=lines.join('\n');
  }
  if(!dirty&&!busy||!hydrated){
    const form=document.querySelector('#feishu-form');
    form.elements.app_id.value=config.app_id;
    form.elements.redirect_uri.value=config.redirect_uri||window.location.origin+'/integrations/feishu/callback';
    form.elements.app_secret.placeholder=status.secret_configured?'密钥已保存，留空保留':'请填写应用 App Secret';
    for(const name of ['enabled','notify_recovery','include_remote_nodes'])form.elements[name].checked=config[name];
    document.querySelector('#feishu-rules').innerHTML=config.rules.map(rule=>{
      const offline=rule.metric==='offline', unit=rule.metric==='gpu_temperature'?'°C':'%';
      return `<article class="feishu-rule" data-feishu-rule="${rule.metric}"><label class="inline-check"><input data-rule-enabled type="checkbox" ${rule.enabled?'checked':''}>${labels[rule.metric]}</label><div class="form-grid"><label>告警阈值 ${offline?'（离线）':unit}<input data-rule-threshold type="number" min="0" max="${rule.metric==='gpu_temperature'?150:100}" step="0.1" value="${rule.threshold}" ${offline?'disabled':''}></label><label>恢复阈值 ${offline?'（在线）':unit}<input data-rule-recovery type="number" min="0" max="150" step="0.1" value="${rule.recovery}" ${offline?'disabled':''}></label><label>持续超限秒数<input data-rule-duration type="number" min="10" max="86400" value="${rule.duration_seconds}" required></label><label>重复提醒间隔秒数<input data-rule-cooldown type="number" min="60" max="86400" value="${rule.cooldown_seconds}" required></label></div></article>`;
    }).join('');
    hydrated=true;
  }
  syncButtons();
}

export async function loadFeishuNotifications() {
  if(!api||loading)return;
  loading=true;
  try{
    status=await api(root);renderStatus();
    const result=await api(root+'/events');
    const kinds={test:'通道测试',alert:'资源告警',reminder:'持续提醒',recovery:'恢复通知'};
    const delivery={sent:'已发送',pending:'等待重试',failed:'发送失败',cancelled:'已取消'};
    document.querySelector('#feishu-events').innerHTML=result.items.length?result.items.map(item=>`<article class="feishu-event"><div><strong>${escapeHtml(kinds[item.kind]||item.kind)} · ${escapeHtml(item.node_label)}</strong><small>${escapeHtml(labels[item.metric]||'机器人通知')} ${item.value==null?'':escapeHtml(item.metric==='offline'?(item.value?'离线':'在线'):String(item.value)+(item.metric==='gpu_temperature'?'°C':'%'))} · ${escapeHtml(new Date(item.created_at*1000).toLocaleString())}</small></div><span>${escapeHtml(delivery[item.delivery]||item.delivery)}</span>${item.error?`<p class="form-error">${escapeHtml(item.error)}</p>`:''}</article>`).join(''):'<div class="empty-state compact">尚无通知记录。配置和绑定不会自动发送消息。</div>';
  }catch(error){document.querySelector('#feishu-status').textContent='通知服务不可用';document.querySelector('#feishu-error').textContent=error.message;}
  finally{loading=false;}
}
