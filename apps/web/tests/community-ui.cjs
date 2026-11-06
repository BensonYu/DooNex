#!/usr/bin/env node
'use strict';
// Build first: pnpm --dir apps/web build
// PLAYWRIGHT_MODULE=/existing/playwright node apps/web/tests/community-ui.cjs
// Every API response is synthetic; production services and providers are never used.
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'../../..'),dist=path.join(root,'apps/web/dist');
const fields=(value,prefix='')=>Object.entries(value).flatMap(([key,item])=>{const name=prefix?prefix+'.'+key:key;return item&&typeof item==='object'&&!Array.isArray(item)?fields(item,name):[{path:name,value:item}];});
const clone=value=>JSON.parse(JSON.stringify(value)),hash=value=>crypto.createHash('sha256').update(value).digest('hex'),hex=letter=>letter.repeat(64);
const now='2026-11-06T20:00:00Z',projectId='00000000-0000-4000-8000-000000000010',submissionId='00000000-0000-4000-8000-000000000001',distributionId='00000000-0000-4000-8000-000000000002',evidenceId='00000000-0000-4000-8000-000000000003';
const archive=Buffer.from('SYNTHETIC IMMUTABLE ARCHIVE'),archiveSha=hash(archive);
const secrets={admin:'synthetic-workbench-admin',submission:'dxc_synthetic_submission_secret_012345678901234567890123',usage:'dxc_synthetic_usage_secret_012345678901234567890123'};
const settings={enabled:false,title:'Synthetic community',description:'Synthetic controlled local community',revision:hex('a'),commercial_enabled:false,limits:{tokens:{daily:20,total:200}},totals:{tokens:0}};
const model={provider:'openai',model_id:'synthetic-model',api_mode:'responses',max_input_tokens:1000,max_output_tokens:100,timeout_seconds:10,input_price_per_million:null,output_price_per_million:null,price_source:null,credential_env:'SYNTHETIC_ONLY',billing_mode:'subscription',allow_private_http:false};
const profile={id:'synthetic',name:'Synthetic',revision:hex('1'),model,credential_available:true,usage_adapter:'none',read_only:false};
const license={status:'supported',spdx:'Apache-2.0',files:[{path:'LICENSE.txt',text:'SYNTHETIC LICENSE BODY\nRetain attribution.\n<img src="https://outside.invalid/license">',sha256:hex('b'),blob_sha:'b'.repeat(40)}],reasons:[]};
const content={title:'Synthetic testing Skill',summary:'Fixed synthetic source; no real authors or results.',category:'synthetic testing',author_label:'Synthetic author <img src="https://outside.invalid/author">',author_statement:'Self-reported attribution only',identity_status:'self_reported',source:{repository:'synthetic/skills',commit:'c'.repeat(40),path:'skills/testing',tree_sha:'d'.repeat(40)},content_digest:hex('e'),files:[{path:'SKILL.md',mode:420,size:64,sha256:hex('f'),blob_sha:'f'.repeat(40)},{path:'LICENSE.txt',mode:420,size:80,sha256:hex('b'),blob_sha:'b'.repeat(40)}],directories:['examples'],instructions:'SYNTHETIC INSTRUCTIONS\n<script>window.remoteExecuted=true</script>\n<img src="https://outside.invalid/instructions">',license,archive_sha256:archiveSha};
const counts={planned:15,attempts:15,completed:15,valid:15,passed:15,failed:0,environment_failure:0,cancelled:0,assisted:0,pending:0,evidence_invalid:0,safety_violations:0,regression_failures:0,missing_slots:0};
const publicEvidence={schema_version:1,source:'controller_verified',release_id:hex('2'),content_digest:content.content_digest,category:content.category,model_id:'synthetic-model',model_fingerprint:hex('3'),taskset_fingerprint:hex('4'),environment_fingerprint:hex('5'),budget_fingerprint:hex('6'),condition_fingerprint:hex('7'),task_count:5,repeats:3,minimum_pass_rate:1,pass_rate:1,stability:'all_passed',counts,batch_counts:counts,usage:{calls:15,tokens:1440,reserved_tokens:0,unknown_calls:0,cost_usd:null,unit_success_cost_usd:null},timing:{successful_median_seconds:2.3,valid_median_seconds:2.3,queued_median_seconds:0.1},eligible:true,successful_timing_complete:true,recommendations:{reliable:true,cost:false,speed:true},verified_at:now,expires_at:'2026-11-15T20:00:00Z'};
let submission=null,release=null,credential=null,evidence=null,revision=0,unknownSubmission=true,unknownPublish=true;
const rev=()=>String(++revision).padStart(64,'0');
const requests=[],violations=[],pageErrors=[],checks=[],registrations=[];
const usage={release_id:hex('2'),window:'lifetime_since_publication',as_of:now,distribution_requests:0,download_response_admissions:0,download_transfer_verified:false,self_reported:{installed:0,used:0,failed:0},controller:{registrations:0,verified_uses:0,successful_acceptances:0,failed:0,cancelled:0,unknown_calls:0},commercial:{enabled:false,settlement_enabled:false,amount_usd:null,earnings_usd:null}};
function check(name,value){assert.ok(value,name);checks.push(name);}
function json(response,status,value){response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));}
function own(value){return Object.fromEntries(['id','input','state','release_id','error','created_at','updated_at'].map(key=>[key,value[key]]));}
const server=http.createServer(async(request,response)=>{
 try{
  const url=new URL(request.url,'http://127.0.0.1'),route=request.method+' '+url.pathname;
  if(!url.pathname.startsWith('/api/')){const target=url.pathname==='/'?path.join(dist,'index.html'):path.join(dist,url.pathname);assert.ok(target.startsWith(dist+path.sep));const raw=fs.readFileSync(target);response.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html'});return response.end(raw);}
  let raw='';for await(const chunk of request){raw+=chunk;assert.ok(raw.length<50000);}const body=raw?JSON.parse(raw):{};
  const authorization=request.headers.authorization||'';requests.push({method:request.method,path:url.pathname,body,auth:authorization===`Bearer ${secrets.admin}`?'admin':authorization===`Bearer ${secrets.submission}`?'submission':authorization===`Bearer ${secrets.usage}`?'usage':authorization?'other':'none'});
  if(url.pathname.startsWith('/api/community/public/'))assert.equal(authorization,'');
  if(url.pathname.startsWith('/api/community/admin/'))assert.equal(authorization,`Bearer ${secrets.admin}`);
  if(route==='GET /api/settings')return json(response,200,{model,credential_available:true,default_budget:{max_calls:1,max_tokens:2000,max_seconds:300,max_cost_usd:null}});
  if(route==='GET /api/projects')return json(response,200,[{id:projectId,name:'Synthetic project',path:'/synthetic/project',head:'a'.repeat(40),dirty:false}]);
  if(route==='GET /api/model-profiles')return json(response,200,{profiles:[profile],default_profile_id:profile.id});
  if(route==='GET /api/arena/recommendations')return json(response,200,{current:[],history:[]});
  if(route==='GET /api/runs')return json(response,200,[]);
  if(route==='GET /api/skills')return json(response,200,{skills:[]});
  if(route==='GET /api/community/admin/settings')return json(response,200,settings);
  if(route==='PUT /api/community/admin/settings'){assert.deepEqual(Object.keys(body).sort(),['description','enabled','expected_revision','title']);assert.equal(body.expected_revision,settings.revision);Object.assign(settings,body,{revision:rev()});delete settings.expected_revision;return json(response,200,settings);}
  if(route==='GET /api/community/admin/tokens')return json(response,200,{credentials:credential?[credential]:[]});
  if(route==='POST /api/community/admin/tokens'){assert.equal(body.label,'Synthetic contributor');assert.equal(body.max_submissions,2);credential={id:'00000000-0000-4000-8000-000000000004',kind:'submission',label:body.label,expires_at:body.expires_at,revoked_at:null,created_at:now,max_submissions:2,submissions_used:0};return json(response,200,{credential,token:secrets.submission,shown_once:true});}
  if(route===`POST /api/community/admin/tokens/${credential?.id}/revoke`){credential.revoked_at=now;return json(response,200,credential);}
  if(route==='GET /api/community/admin/submissions')return json(response,200,{submissions:submission?[submission]:[]});
  if(route===`GET /api/community/admin/submissions/${submissionId}`)return json(response,200,submission);
  if(route===`POST /api/community/admin/submissions/${submissionId}/read`){assert.equal(submission.state,'submitted');assert.equal(body.expected_revision,submission.revision);assert.match(body.request_id,/^[a-f0-9-]{36}$/);submission.state='source_ready';submission.revision=rev();submission.discovery={id:'00000000-0000-4000-8000-000000000005',source:content.source,budget:{max_requests:32,requests_used:4,remaining_requests:28,max_bytes:100000,bytes_used:144,bytes_reserved:0,remaining_bytes:99856},requests:[],candidates:[{id:hex('8'),path:'skills/testing',file_count:2,entry_count:3,total_bytes:144,tree_sha:content.source.tree_sha}]};return json(response,200,submission);}
  if(route===`POST /api/community/admin/submissions/${submissionId}/preview`){assert.equal(submission.state,'source_ready');assert.equal(body.expected_revision,submission.revision);assert.equal(body.candidate_id,hex('8'));submission.state='preview_ready';submission.revision=rev();submission.preview={sha256:hex('9'),public:clone(content),discovery_preview_sha256:hex('a')};return json(response,200,submission);}
  if(route===`POST /api/community/admin/submissions/${submissionId}/review`){assert.equal(submission.state,'preview_ready');assert.equal(body.expected_revision,submission.revision);assert.equal(body.preview_sha256,submission.preview.sha256);assert.equal(body.status,'approved');assert.ok(body.note);submission.state='approved';submission.revision=rev();submission.review={...body,at:now};return json(response,200,submission);}
  if(route===`POST /api/community/admin/submissions/${submissionId}/publish`){assert.equal(submission.state,'approved');assert.equal(body.expected_revision,submission.revision);assert.equal(body.preview_sha256,submission.preview.sha256);assert.ok(body.confirm_public_source&&body.confirm_license&&body.confirm_publish);release={id:hex('2'),state:'published',...clone(content),published_at:now,review:{reviewer_label:'Synthetic maintainer',approved_at:now},quality:{controller_verified:false,evidence_count:0}};submission.state='published';submission.release_id=release.id;submission.revision=rev();if(unknownPublish){unknownPublish=false;return json(response,502,{detail:'PRIVATE_PUBLISH_ERROR_SENTINEL'});}return json(response,200,release);}
  if(route==='GET /api/community/admin/releases')return json(response,200,{releases:release?[release]:[]});
  if(route===`POST /api/community/admin/releases/${release?.id}/withdraw`){assert.ok(body.reason);release.state='withdrawn';return json(response,200,release);}
  if(route==='GET /api/community/admin/evidence')return json(response,200,{evidence:evidence?[evidence,{id:'00000000-0000-4000-8000-000000000009',state:'failed',public:null,preview_sha256:null,private_binding_digest:null,error:'community_preparation_failed',public_fields:[],revision:hex('d')}]:[]});
  if(route==='POST /api/community/admin/evidence/prepare'){assert.equal(body.release_id,release.id);assert.ok(body.evaluation_id&&body.arm_id);evidence={id:evidenceId,state:'prepared',revision:rev(),preview_sha256:hex('b'),public:clone(publicEvidence),private_binding_digest:hex('c'),public_fields:fields(publicEvidence)};return json(response,200,evidence);}
  if(route===`POST /api/community/admin/evidence/${evidenceId}/publish`){assert.equal(body.expected_revision,evidence.revision);assert.equal(body.preview_sha256,evidence.preview_sha256);assert.ok(body.confirm_public_fields&&body.confirm_publish);evidence.state='published';evidence.revision=rev();return json(response,200,evidence);}
  if(route===`POST /api/community/admin/evidence/${evidenceId}/withdraw`){assert.equal(body.expected_revision,evidence.revision);evidence.state='withdrawn';evidence.revision=rev();return json(response,200,evidence);}
  if(route==='GET /api/community/admin/verified-usage')return json(response,200,{registrations});
  if(route==='POST /api/community/admin/verified-usage'){assert.equal(body.release_id,release.id);assert.ok(body.confirm_register);const value={id:'00000000-0000-4000-8000-000000000006',...body,public:{source:'controller_registration',outcome:'verified_success',controller_verified:true,successful_acceptance:true,unknown_calls:0,amount_usd:null},private_binding_digest:hex('c'),revision:rev()};registrations.push(value);usage.controller={...usage.controller,registrations:1,verified_uses:1,successful_acceptances:1};return json(response,200,value);}
  if(route==='GET /api/community/public/site'){if(!settings.enabled)return json(response,404,{detail:{code:'community_disabled'}});return json(response,200,{title:settings.title,description:settings.description,commercial:{enabled:false},identity_policy:'self reported',leaderboard_reason:'no_controller_verified_evidence'});}
  if(route==='GET /api/community/public/releases')return json(response,200,{releases:release?.state==='published'?[release]:[]});
  if(route===`GET /api/community/public/releases/${hex('2')}`){if(release?.state!=='published')return json(response,404,{detail:{code:'community_release_not_found'}});return json(response,200,release);}
  if(route===`GET /api/community/public/releases/${hex('2')}/usage`)return json(response,200,usage);
  if(route==='GET /api/community/public/evidence')return json(response,200,{evidence:evidence?.state==='published'?[{...evidence.public,id:evidence.id,published_at:now}]:[]});
  if(route==='GET /api/community/public/leaderboard')return json(response,200,{groups:evidence?.state==='published'?[{...Object.fromEntries(['condition_fingerprint','category','model_id','model_fingerprint','taskset_fingerprint','environment_fingerprint','budget_fingerprint'].map(key=>[key,publicEvidence[key]])),entries:[{...publicEvidence,id:evidenceId,published_at:now}],recommendations:{reliable:[evidenceId],cost:[],speed:[evidenceId]}}]:[]});
  if(route===`POST /api/community/public/releases/${hex('2')}/distributions`){assert.equal(release.state,'published');assert.match(body.request_id,/^[a-f0-9-]{36}$/);usage.distribution_requests++;return json(response,200,{receipt:{id:distributionId,release_id:release.id,source:'distribution_request',created_at:now,installation_verified:false,content_digest:release.content_digest,archive_sha256:archiveSha},usage_token:secrets.usage,shown_once:true});}
  if(route===`GET /api/community/public/distributions/${distributionId}/download`){usage.download_response_admissions++;response.writeHead(200,{'Content-Type':'application/gzip'});return response.end(archive);}
  if(route==='GET /api/community/submissions'){assert.equal(authorization,`Bearer ${secrets.submission}`);return json(response,200,{submissions:submission?[own(submission)]:[]});}
  if(route==='POST /api/community/submissions'){assert.equal(authorization,`Bearer ${secrets.submission}`);assert.match(body.request_id,/^[a-f0-9-]{36}$/);assert.equal(body.repository,'synthetic/skills');submission={id:submissionId,input:{...body,source:{repository:body.repository,ref:body.ref,prefix:body.prefix}},state:'submitted',revision:rev(),operation:null,discovery:null,preview:null,review:null,release_id:null,error:null,created_at:now,updated_at:now};credential.submissions_used++;if(unknownSubmission){unknownSubmission=false;return json(response,502,{detail:'PRIVATE_CONTRIBUTOR_ERROR_SENTINEL'});}return json(response,200,own(submission));}
  if(route==='POST /api/community/submissions/usage'){assert.equal(authorization,`Bearer ${secrets.usage}`);assert.match(body.event_id,/^[a-f0-9-]{36}$/);usage.self_reported[body.outcome]++;return json(response,200,{...body,release_id:release.id,distribution_id:distributionId,source:'self_reported',controller_verified:false,reported_at:now});}
  violations.push('Unexpected route '+route);return json(response,500,{detail:'Unexpected synthetic route'});
 }catch(error){violations.push(error.message);return json(response,500,{detail:'Synthetic contract violation'});}
});

(async()=>{
 const output=process.env.DOONEX_UI_EVIDENCE_DIR||path.join(require('node:os').tmpdir(),'doonex-community-ui-evidence');
 fs.mkdirSync(output,{recursive:true});
 let browser;
 const count=(method,route)=>requests.filter(item=>item.method===method&&item.path===route).length;
 const textOf=page=>page.locator('body').innerText();
 const button=(page,name)=>page.getByRole('button',{name,exact:true});
 const expiry=new Date(Date.now()+2*86400000).toISOString().slice(0,16);
 const screenshots=[];
 const screenshot=async(page,name)=>{
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const file=path.join(output,name+'.png');
  await page.screenshot({path:file,fullPage:false});screenshots.push(file);
  check(name+' has no horizontal document overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 };
 try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const context=await browser.newContext({viewport:{width:1280,height:900},acceptDownloads:true});
  await context.route('**/*',async route=>{
   const url=route.request().url();
   if(!url.startsWith(origin+'/')&&!url.startsWith('blob:')&&!url.startsWith('data:')){violations.push('External browser request');return route.abort();}
   return route.continue();
  });
  context.on('page',page=>page.on('pageerror',error=>pageErrors.push(error.message)));
  const publicPage=await context.newPage();
  await publicPage.addInitScript(value=>{sessionStorage.setItem('doonex.token',value);sessionStorage.setItem('synthetic-private','PRIVATE_WORKBENCH_SENTINEL');},secrets.admin);
  await publicPage.goto(origin+'/#community');
  await publicPage.getByRole('heading',{name:'本站社区未启用',exact:true}).waitFor();
  check('default closed public entry only reads public site',requests.every(item=>item.path==='/api/community/public/site'&&item.method==='GET'&&item.auth==='none'));
  check('public closed page exposes no private workbench sentinel',!(await textOf(publicPage)).includes('PRIVATE_WORKBENCH_SENTINEL'));
  await screenshot(publicPage,'desktop-closed');
  const admin=await context.newPage();
  await admin.goto(origin+'/');
  await admin.locator('#access-token').fill(secrets.admin);
  await button(admin,'连接工作台').click();
  await button(admin,'社区管理').click();
  await admin.getByText('社区当前关闭，公开访问不会返回私有工作台数据。',{exact:true}).waitFor();
  check('admin reads disabled community without writing',count('PUT','/api/community/admin/settings')===0);
  await admin.getByLabel('启用本站社区公开读取与有限投稿',{exact:true}).check();
  await button(admin,'保存站点设置').click();
  await admin.getByText('社区读取与有限投稿已启用。',{exact:true}).waitFor();
  check('site enabling is one explicit revision-bound write',count('PUT','/api/community/admin/settings')===1&&settings.enabled);
  await admin.locator('#community-token-label').fill('Synthetic contributor');
  await admin.locator('#community-token-expiry').fill('2020-01-01T12:00');
  await admin.locator('#community-token-capacity').fill('2');
  check('past token expiry is disabled',await button(admin,'发放一次性投稿令牌').isDisabled());
  await admin.locator('#community-token-expiry').fill(expiry);
  await button(admin,'发放一次性投稿令牌').click();
  await admin.locator('.community-secret').filter({hasText:secrets.submission}).waitFor();
  check('credential is issued once with finite capacity',count('POST','/api/community/admin/tokens')===1&&credential.max_submissions===2);
  await button(admin,'已保存，隐藏令牌').click();
  check('one-time submission secret is hidden explicitly',!(await textOf(admin)).includes(secrets.submission));
  await admin.reload();
  await button(admin,'社区管理').click();
  await admin.getByText('Synthetic contributor',{exact:true}).waitFor();
  check('credential list and reload do not recover secret',!(await textOf(admin)).includes(secrets.submission));
  check('community credentials do not enter browser storage',await admin.evaluate(values=>![...Object.values(localStorage),...Object.values(sessionStorage)].some(value=>values.some(secret=>value.includes(secret))),[secrets.submission,secrets.usage]));
  await button(publicPage,'读取公开目录').click();
  await button(publicPage,'作者投稿').click();
  await publicPage.locator('#community-contributor-token').fill(secrets.submission);
  const fillSubmission=async()=>{
   for(const [key,value]of Object.entries({repository:'synthetic/skills',ref:'main',prefix:'skills/testing',title:content.title,category:content.category,author_label:'Synthetic author',summary:content.summary,statement:'Self-reported attribution only'}))await publicPage.locator('#community-submit-'+key).fill(value);
  };
  await fillSubmission();
  await button(publicPage,'提交一份公开来源投稿').dblclick();
  await publicPage.getByRole('alert').filter({hasText:'HTTP 502'}).waitFor();
  check('unknown contributor result never replays on double click',count('POST','/api/community/submissions')===1);
  check('provider error body is redacted',!(await textOf(publicPage)).includes('PRIVATE_CONTRIBUTOR_ERROR_SENTINEL'));
  check('unknown contributor marker binds original request id',await publicPage.evaluate(id=>JSON.parse(localStorage.getItem('doonex.community.unknown.contributor')).intent_id===id,submission.input.request_id));
  await publicPage.reload();
  await button(publicPage,'作者投稿').click();
  await publicPage.locator('#community-contributor-token').fill(secrets.submission);
  await fillSubmission();
  check('unknown contributor mutation stays blocked after reload',await button(publicPage,'提交一份公开来源投稿').isDisabled());
  await button(publicPage,'读取本人回执').click();
  await publicPage.getByText(submissionId,{exact:true}).waitFor();
  check('explicit own receipt resolves matching saved intent',await button(publicPage,'提交一份公开来源投稿').isEnabled());
  check('contributor submission alone never reads source',count('POST',`/api/community/admin/submissions/${submissionId}/read`)===0);
  await button(admin,'读取社区管理记录').click();
  await admin.getByRole('navigation',{name:'社区投稿记录'}).getByRole('button').click();
  await button(admin,'明确读取一次公开来源').click();
  await button(admin,'保存一次固定公开预览').click();
  await button(admin,'批准社区预览').waitFor();
  check('fixed source and preview are independent explicit bounded writes',count('POST',`/api/community/admin/submissions/${submissionId}/read`)===1&&count('POST',`/api/community/admin/submissions/${submissionId}/preview`)===1);
  await admin.getByText('完整指令原文',{exact:true}).click();
  await admin.locator('summary').filter({hasText:/^LICENSE\.txt$/}).click();
  await admin.getByText('文件清单与摘要（2）',{exact:true}).click();
  check('source instructions and license are inert plain text',await admin.locator('img').count()===0&&await admin.evaluate(()=>window.remoteExecuted!==true));
  check('fixed digest and manifest are visible',(await textOf(admin)).includes(content.content_digest)&&(await textOf(admin)).includes(content.files[0].sha256)&&(await textOf(admin)).includes('Retain attribution.'));
  check('review needs explicit reviewer and private note',await button(admin,'批准社区预览').isDisabled());
  await admin.locator('#community-reviewer').fill('Synthetic maintainer');
  await admin.locator('#community-review-note').fill('Synthetic controlled source review.');
  await button(admin,'批准社区预览').click();
  await button(admin,'发布此固定快照').waitFor();
  check('community approval does not publish or approve Library',release===null&&!requests.some(item=>item.method!=='GET'&&item.path.startsWith('/api/skills')));
  check('publication needs three independent confirmations',await button(admin,'发布此固定快照').isDisabled());
  for(const name of ['我已核对公开来源、固定提交和全部公开字段。','我已核对许可正文、版权和归属，确认本次分发依据。','我明确授权将此固定快照发布到本站公开目录。'])await admin.getByLabel(name,{exact:true}).check();
  await button(admin,'发布此固定快照').dblclick();
  await admin.getByRole('alert').filter({hasText:'HTTP 502'}).waitFor();
  check('unknown publish response never repeats publication',count('POST',`/api/community/admin/submissions/${submissionId}/publish`)===1);
  check('unknown publish hides raw body and blocks further writes',!(await textOf(admin)).includes('PRIVATE_PUBLISH_ERROR_SENTINEL')&&await button(admin,'发布此固定快照').isDisabled());
  await button(admin,'读取社区管理记录').click();
  await admin.getByText('关联发布 '+release.id+'。发布状态以发布记录为准。',{exact:true}).waitFor();
  check('explicit admin read recovers published release binding',submission.release_id===release.id&&count('POST',`/api/community/admin/submissions/${submissionId}/publish`)===1);
  await publicPage.reload();
  await publicPage.getByRole('button',{name:'查看 '+content.title,exact:true}).click();
  const publicDetails=publicPage.getByRole('region',{name:'公开版本详情'});
  await publicDetails.getByRole('heading',{name:'确切发布版本',exact:true}).waitFor();
  check('public catalog clearly labels self-reported identity and absent quality',(await textOf(publicPage)).includes('身份自报')&&(await textOf(publicPage)).includes('未声明质量通过'));
  check('leaderboard is empty without eligible verified snapshots',(await textOf(publicPage)).includes('榜单为空'));
  await publicDetails.getByText('完整指令原文',{exact:true}).click();
  await publicDetails.locator('summary').filter({hasText:/^LICENSE\.txt$/}).click();
  check('public source text is inert and retains license',await publicPage.locator('img').count()===0&&(await textOf(publicPage)).includes('Retain attribution.'));
  await button(publicPage,'创建本次分发回执').dblclick();
  await publicPage.getByText(secrets.usage,{exact:true}).waitFor();
  check('double click creates a single unverified distribution receipt',count('POST',`/api/community/public/releases/${release.id}/distributions`)===1&&usage.distribution_requests===1);
  const downloadEvent=publicPage.waitForEvent('download');
  await button(publicPage,'下载摘要校验的固定包').click();
  const download=await downloadEvent;const downloadFile=await download.path();
  check('download bytes match frozen SHA-256 before save',hash(fs.readFileSync(downloadFile))===archiveSha);
  check('download admission is separate from completion and installation',usage.download_response_admissions===1&&usage.self_reported.installed===0&&usage.controller.verified_uses===0);
  await button(publicPage,'隐藏使用自报令牌').click();
  check('one-time usage token is hidden without storage',!(await textOf(publicPage)).includes(secrets.usage)&&await publicPage.evaluate(secret=>!Object.values(localStorage).some(value=>value.includes(secret)),secrets.usage));
  await button(publicPage,'使用自报').click();
  await publicPage.locator('#community-usage-token').fill(secrets.usage);
  await button(publicPage,'提交一次使用自报').click();
  await publicPage.getByText('已保存客户端自报：used · 非控制器核验',{exact:false}).waitFor();
  check('usage self report uses only scoped usage bearer',requests.find(item=>item.path==='/api/community/submissions/usage').auth==='usage'&&usage.self_reported.used===1&&usage.controller.verified_uses===0);
  await admin.locator('#community-evidence-release').selectOption(release.id);
  await admin.locator('#community-evaluation-id').fill('PRIVATE_EVALUATION_SENTINEL');
  await admin.locator('#community-arm-id').fill('PRIVATE_ARM_SENTINEL');
  await admin.locator('#community-evidence-expiry').fill(expiry);
  await button(admin,'准备公开字段预览').click();
  await admin.getByRole('heading',{name:'将公开的每一个字段',exact:true}).waitFor();
  check('prepared evidence exposes full public field whitelist',await admin.locator('.community-public-fields > div').count()===fields(publicEvidence).length);
  check('evidence publication is separately confirmed',await button(admin,'公开此证据快照').isDisabled());
  await admin.getByLabel('我已逐字段核对全部公开证据内容与固定版本。',{exact:true}).check();
  await admin.getByLabel('我明确授权公开本证据快照。',{exact:true}).check();
  evidence.revision=rev();
  await button(admin,'读取保存的证据').click();
  await admin.waitForFunction(()=>[...document.querySelectorAll('label')].filter(label=>label.textContent==='我已逐字段核对全部公开证据内容与固定版本。'||label.textContent==='我明确授权公开本证据快照。').every(label=>!label.querySelector('input').checked));
  check('changed evidence revision resets both public confirmations',!await admin.getByLabel('我已逐字段核对全部公开证据内容与固定版本。',{exact:true}).isChecked()&&!await admin.getByLabel('我明确授权公开本证据快照。',{exact:true}).isChecked());
  await admin.getByRole('navigation',{name:'公共证据记录'}).getByRole('button').filter({hasText:'已保留失败记录'}).click();
  await admin.getByText('community_preparation_failed。保留失败记录。',{exact:true}).waitFor();
  check('failed evidence with null preview is retained and safe',await button(admin,'公开此证据快照').count()===0);
  await admin.getByRole('navigation',{name:'公共证据记录'}).getByRole('button').filter({hasText:'待公开字段确认'}).click();
  await admin.getByLabel('我已逐字段核对全部公开证据内容与固定版本。',{exact:true}).check();
  await admin.getByLabel('我明确授权公开本证据快照。',{exact:true}).check();
  await button(admin,'公开此证据快照').click();
  await button(admin,'撤回此公开证据').waitFor();
  check('public evidence publishes once with preview and revision binding',evidence.state==='published'&&count('POST',`/api/community/admin/evidence/${evidenceId}/publish`)===1);
  await admin.getByText('私有控制器使用登记',{exact:true}).click();
  await admin.locator('#community-usage-run').fill('PRIVATE_RUN_SENTINEL');
  await admin.locator('#community-usage-distribution').fill(distributionId);
  await admin.getByLabel('我已核对固定发布、实际 Run 和原始证据，确认登记。',{exact:true}).check();
  await button(admin,'登记核验使用').click();
  await admin.getByText('controller_registration',{exact:false}).waitFor();
  check('private controller registration remains distinct from self reports',registrations.length===1&&usage.controller.verified_uses===1&&usage.self_reported.used===1);
  await button(publicPage,'固定版本目录').click();
  await button(publicPage,'读取公开目录').click();
  await publicPage.getByRole('region',{name:'按条件的控制器核验榜单'}).getByRole('heading',{name:'synthetic testing · synthetic-model',exact:true,level:3}).waitFor();
  check('public leaderboard groups frozen fingerprints',await publicPage.getByRole('region',{name:'按条件的控制器核验榜单'}).getByText(publicEvidence.condition_fingerprint,{exact:true}).count()===1);
  check('unknown cost leaves cost recommendation empty',(await textOf(publicPage)).includes('费用 未知')&&(await textOf(publicPage)).includes('无合格记录，保持空缺'));
  const publicText=await textOf(publicPage);
  check('rendered public evidence contains no private identifiers',!['PRIVATE_EVALUATION_SENTINEL','PRIVATE_ARM_SENTINEL','PRIVATE_RUN_SENTINEL',evidence.private_binding_digest].some(value=>publicText.includes(value)));
  await publicPage.setViewportSize({width:390,height:844});
  await publicPage.evaluate(()=>scrollTo(0,0));await screenshot(publicPage,'mobile-catalog');
  await button(publicPage,'查看 '+content.title).click();
  await publicPage.getByRole('heading',{name:'确切发布版本',exact:true}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(publicPage,'mobile-release');
  await publicPage.getByRole('region',{name:'公开版本详情'}).getByText('完整指令原文',{exact:true}).click();
  await publicPage.getByRole('region',{name:'公开版本详情'}).locator('summary').filter({hasText:/^LICENSE\.txt$/}).click();
  await publicPage.getByRole('region',{name:'公开版本详情'}).getByText('完整指令原文',{exact:true}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(publicPage,'mobile-source-license');
  await publicPage.getByRole('region',{name:'按条件的控制器核验榜单'}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(publicPage,'mobile-leaderboard');
  await admin.setViewportSize({width:390,height:844});
  await admin.getByRole('heading',{name:'社区管理',exact:true}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(admin,'mobile-admin');
  await admin.getByRole('heading',{name:'独立公共证据快照',exact:true}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(admin,'mobile-evidence');
  await admin.getByRole('heading',{name:'将公开的每一个字段',exact:true}).evaluate(element=>element.scrollIntoView({block:'start'}));await screenshot(admin,'mobile-public-fields');
  await button(admin,'撤回此公开证据').click();
  await admin.getByText(evidenceId,{exact:true}).first().waitFor();
  check('withdrawing evidence keeps historical record',evidence.state==='withdrawn'&&count('POST',`/api/community/admin/evidence/${evidenceId}/withdraw`)===1);
  await admin.locator('#community-release-manage').selectOption(release.id);
  await admin.locator('#community-withdraw-reason').fill('Synthetic controlled withdrawal.');
  await button(admin,'下架此发布').click();
  await button(admin,'下架此发布').waitFor({state:'visible'});
  check('withdrawal keeps fixed history and disables new admission',release.state==='withdrawn'&&await button(admin,'下架此发布').isDisabled());
  await button(publicPage,'读取公开目录').click();
  await publicPage.getByText('暂无已发布版本。',{exact:true}).waitFor();
  check('withdrawn release and evidence disappear from public catalog',await button(publicPage,'创建本次分发回执').count()===0&&(await textOf(publicPage)).includes('榜单为空'));
  await button(admin,'撤销凭据').click();
  await admin.getByText('投稿凭据 · 到期',{exact:false}).waitFor();
  check('credential can be revoked without recovering secret',!!credential.revoked_at&&await button(admin,'撤销凭据').isDisabled());
  // This public intent has no recovery endpoint. Catalog reads must not clear it.
  await publicPage.evaluate(()=>localStorage.setItem('doonex.community.unknown.public-distribution',JSON.stringify({kind:'创建本次分发回执',intent_id:'00000000-0000-4000-8000-000000000099',at:new Date().toISOString()})));
  await publicPage.reload();
  await publicPage.getByText('公开目录读取无法恢复分发或下载回执。',{exact:false}).waitFor();
  await button(publicPage,'读取公开目录').click();
  check('catalog reads cannot clear unknown distribution admission',await publicPage.evaluate(()=>!!localStorage.getItem('doonex.community.unknown.public-distribution')));
  const noStorage=await context.newPage();
  await noStorage.addInitScript(value=>{sessionStorage.setItem('doonex.token',value);const original=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith('doonex.community.unknown.'))throw new DOMException('Synthetic storage blocked','SecurityError');return original.call(this,key,value);};},secrets.admin);
  await noStorage.goto(origin+'/');await button(noStorage,'社区管理').click();
  await noStorage.locator('#community-title').fill('Storage failure must block');
  const settingsWrites=count('PUT','/api/community/admin/settings');await button(noStorage,'保存站点设置').click();
  await noStorage.getByRole('alert').filter({hasText:'浏览器无法保存操作意图'}).waitFor();
  check('unavailable durable browser storage blocks writes before requests',count('PUT','/api/community/admin/settings')===settingsWrites);
  check('public reads never inherit admin authorization',requests.filter(item=>item.path.startsWith('/api/community/public/')).every(item=>item.auth==='none'));
  check('no Run Arena generation or background writes occur',requests.filter(item=>item.method!=='GET').every(item=>item.path.startsWith('/api/community/')));
  check('no external requests contract violations or runtime errors',violations.length===0&&pageErrors.length===0);
  const sourceFiles=['main.tsx','style.css','community-api.ts','community-common.tsx','community-admin.tsx','community-public.tsx','community-evidence.tsx'];
  const result={scope:'synthetic local HTTP/browser only; no real provider product DB or upstream requests',source_commit:execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim(),source_sha256:Object.fromEntries(sourceFiles.map(file=>[file,hash(fs.readFileSync(path.join(root,'apps/web/src',file)))])),checks,requests:requests.map(({method,path,auth})=>({method,path,auth})),page_errors:pageErrors,violations,screenshots};
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify({status:'passed',checks:checks.length,evidence:path.join(output,'result.json'),screenshots:screenshots.length}));
 }catch(error){
  fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({error:error.stack,checks,violations,pageErrors,requests:requests.map(({method,path,auth})=>({method,path,auth}))},null,2)+'\n');throw error;
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
