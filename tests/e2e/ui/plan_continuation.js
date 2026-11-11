// Browser check for patch 0012 (plan continuation). Needs Node with the
// playwright package and a running install; the chat stream is answered by
// this script, so no model is needed.
//
//   U=admin P='<admin password>' node tests/e2e/ui/plan_continuation.js
//
// Checks: Continue resends the approved plan with its ticks; the step limit
// continues on its own at most 3 times; Stop here cancels that; unrelated
// messages, finished plans and other chats' plans are never resent.
const { chromium } = require('playwright');
const BASE = process.env.WINGS_URL || 'http://127.0.0.1:7011';
const USER = process.env.U, PW = process.env.P;
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const bodies = [];
  let step = 0;
  await page.route('**/api/chat_stream', async (route) => {
    const body = route.request().postDataBuffer() || Buffer.from('');
    const text = body.toString('latin1');
    const field = (name) => { const m = text.match(new RegExp('name="' + name + '"\\r\\n\\r\\n([\\s\\S]*?)\\r\\n--')); return m ? m[1] : null; };
    bodies.push({ message: field('message'), approved_plan: field('approved_plan'), session: field('session_id') || field('session') });
    step += 1;
    const events = step >= 2
      ? [{ delta: 'Did step b.' }, { type: 'rounds_exhausted', rounds: 40 }]
      : [{ delta: 'ok ' + step }];
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
  await page.evaluate((sid) => localStorage.setItem('wings-approved-plan', JSON.stringify({ session: sid, plan: '- [x] step a\n- [ ] step b\n- [ ] step c' })), sid);
  await send('Continue');
  await page.waitForTimeout(14000);
  const label1 = await page.evaluate(() => document.querySelector('.rounds-exhausted-label')?.textContent || '');
  console.log('requests after cap:', bodies.length, '|', label1);
  await page.reload(); await page.waitForSelector('#message');
  await page.evaluate((sid) => window.sessionModule.getCurrentSessionId() === sid, sid);
  const n0 = bodies.length;
  await page.fill('#message', 'continue'); await page.click('.send-btn');
  await page.waitForSelector('text=Stop here', { timeout: 5000 });
  const sid2 = await page.evaluate(() => window.sessionModule.getCurrentSessionId());
  await page.click('text=Stop here').catch(e => console.log('no stop button', String(e).slice(0, 80)));
  await page.waitForTimeout(4000);
  const label2 = await page.evaluate(() => document.querySelector('.rounds-exhausted-label')?.textContent || '');
  console.log('after Stop here, new requests:', bodies.length - n0, '|', label2, '| same chat:', sid2 === sid);
  await send('What is the weather like?');   // not a continuation: no plan
  await page.evaluate((sid) => localStorage.setItem('wings-approved-plan', JSON.stringify({ session: sid, plan: '- [x] step a\n- [x] step b' })), sid);
  await send('continue');                     // finished plan: not resent
  await page.evaluate(() => localStorage.setItem('wings-approved-plan', JSON.stringify({ session: 'other-chat', plan: '- [ ] x' })));
  await send('continue');                     // another chat's plan: not resent
  const fail = [];
  if (bodies.slice(6).some(b => b.approved_plan)) fail.push('a plan was sent where it must not be');
  if (bodies.length !== 9) fail.push(`expected 9 requests, saw ${bodies.length}`);
  if (!bodies[1] || !bodies[1].approved_plan) fail.push('Continue did not resend the plan');
  if (bodies.slice(2, 5).some(b => !b.approved_plan)) fail.push('automatic continues did not carry the plan');
  console.log(fail.length ? 'FAIL: ' + fail.join('; ') : 'PASS plan continuation');
  if (fail.length) process.exitCode = 1;
  
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
