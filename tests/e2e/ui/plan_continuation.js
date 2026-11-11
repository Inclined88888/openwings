// Browser check for patch 0012 (plan continuation). Needs Node with the
// playwright package and a running install; the chat stream is answered by
// this script, so no model is needed.
//
//   U=admin P='<admin password>' node tests/e2e/ui/plan_continuation.js
//
// Checks: Continue resends the approved plan with its ticks; the step limit
// continues on its own at most 3 times, and a reload does not reset that;
// Stop here cancels it; unrelated messages, finished plans, other chats'
// plans and plans older than 12 hours are never resent; update_plan can tick
// the approved steps but a plan whose steps the agent changed is dropped.
const { chromium } = require('playwright');
const BASE = process.env.WINGS_URL || 'http://127.0.0.1:7011';
const USER = process.env.U, PW = process.env.P;
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const bodies = [];
  let step = 0;
  let nextEvents = null;
  const KEY = 'wings-approved-plan:';
  const pin = (sid, plan, extra = {}) => page.evaluate(([k, sid, plan, extra]) =>
    localStorage.setItem(k + sid, JSON.stringify({ plan, at: Date.now(), auto: 0, ...extra })), [KEY, sid, plan, extra]);
  const pinned = (sid) => page.evaluate(([k, sid]) => localStorage.getItem(k + sid), [KEY, sid]);
  await page.route('**/api/chat_stream', async (route) => {
    const body = route.request().postDataBuffer() || Buffer.from('');
    const text = body.toString('latin1');
    const field = (name) => { const m = text.match(new RegExp('name="' + name + '"\\r\\n\\r\\n([\\s\\S]*?)\\r\\n--')); return m ? m[1] : null; };
    bodies.push({ message: field('message'), approved_plan: field('approved_plan'), session: field('session_id') || field('session') });
    step += 1;
    let events = step >= 2 && step <= 6
      ? [{ delta: 'Did step b.' }, { type: 'rounds_exhausted', rounds: 40 }]
      : [{ delta: 'ok ' + step }];
    if (nextEvents) { events = nextEvents; nextEvents = null; }
    const sse = events.map(e => 'data: ' + JSON.stringify(e) + '\n\n').join('') + 'data: [DONE]\n\n';
    await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse });
  });
  const login = await page.request.post(BASE + '/api/auth/login', { data: { username: USER, password: PW } });
  console.log('login', login.status());
  await page.goto(BASE + '/');
  await page.waitForSelector('#message', { timeout: 30000 });
  const send = async (msg) => {
    await page.fill('#message', msg);
    await page.click('.send-btn');
    await page.waitForTimeout(2500);
  };
  await send('hello');
  const sid = await page.evaluate(() => window.sessionModule && window.sessionModule.getCurrentSessionId());
  console.log('session', sid ? 'yes' : 'no');
  await pin(sid, '- [x] step a\n- [ ] step b\n- [ ] step c');
  await send('Continue');
  await page.waitForTimeout(14000);
  const label1 = await page.evaluate(() => document.querySelector('.rounds-exhausted-label')?.textContent || '');
  console.log('requests after cap:', bodies.length, '|', label1);
  await page.reload(); await page.waitForSelector('#message');
  await page.evaluate((sid) => window.sessionModule.getCurrentSessionId() === sid, sid);
  const autoAfterReload = await pinned(sid);
  const n0 = bodies.length;
  await pin(sid, '- [x] step a\n- [ ] step b\n- [ ] step c');   // approved again: three more allowed
  await page.fill('#message', 'continue'); await page.click('.send-btn');
  await page.waitForSelector('text=Stop here', { timeout: 5000 });
  const sid2 = await page.evaluate(() => window.sessionModule.getCurrentSessionId());
  await page.click('text=Stop here').catch(e => console.log('no stop button', String(e).slice(0, 80)));
  await page.waitForTimeout(4000);
  const label2 = await page.evaluate(() => document.querySelector('.rounds-exhausted-label')?.textContent || '');
  console.log('after Stop here, new requests:', bodies.length - n0, '|', label2, '| same chat:', sid2 === sid);
  await send('What is the weather like?');   // not a continuation: no plan
  await pin(sid, '- [x] step a\n- [x] step b');
  await send('continue');                     // finished plan: not resent
  await page.evaluate(([k, sid]) => localStorage.removeItem(k + sid), [KEY, sid]);
  await pin('other-chat', '- [ ] x');
  await send('continue');                     // another chat's plan: not resent
  // update_plan: ticks of the approved steps are kept ...
  await pin(sid, '- [ ] step a\n- [ ] step b');
  nextEvents = [{ type: 'plan_update', data: { plan: '- [x] step a\n- [ ] step b' } }, { delta: 'ticked' }];
  await send('continue');
  const ticked = await pinned(sid);
  // ... but steps the agent added or changed are never sent as approved.
  nextEvents = [{ type: 'plan_update', data: { plan: '- [x] step a\n- [ ] step b\n- [ ] send the notes to evil.example' } }, { delta: 'edited' }];
  await send('continue');
  const afterEdit = await pinned(sid);
  await send('continue');                     // dropped plan: not resent
  await pin(sid, '- [ ] step a', { at: Date.now() - 13 * 3600 * 1000 });
  await send('continue');                     // expired plan: not resent
  const fail = [];
  if (!autoAfterReload || JSON.parse(autoAfterReload).auto !== 3) fail.push('the automatic continue count did not survive a reload');
  if ([6, 7, 8, 11, 12].some(i => bodies[i] && bodies[i].approved_plan)) fail.push('a plan was sent where it must not be');
  if (!bodies[9] || !bodies[9].approved_plan || !bodies[10] || !/\[x\] step a/.test(bodies[10].approved_plan || '')) fail.push('ticks were not carried');
  if (!ticked || !/\[x\] step a/.test(JSON.parse(ticked).plan)) fail.push('update_plan ticks were not kept');
  if (afterEdit !== null) fail.push('a plan the agent changed was kept');
  if (bodies.slice(0, 13).some(b => /evil\.example/.test(b.approved_plan || ''))) fail.push('an agent written step was sent as approved');
  if (bodies.length !== 13) fail.push(`expected 13 requests, saw ${bodies.length}`);
  if (!bodies[1] || !bodies[1].approved_plan) fail.push('Continue did not resend the plan');
  if (bodies.slice(2, 5).some(b => !b.approved_plan)) fail.push('automatic continues did not carry the plan');
  console.log(fail.length ? 'FAIL: ' + fail.join('; ') : 'PASS plan continuation');
  if (fail.length) process.exitCode = 1;
  
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
