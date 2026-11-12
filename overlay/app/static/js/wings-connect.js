// Wings (patch 0021): Connect. Use what people already have, in one click.
//
// * First run: while no model endpoint has any models, the welcome screen
//   shows a short "Connect your AI" panel in #welcome-setup (left empty
//   upstream) to an admin, and one line saying whom to ask to a member.
// * Settings > Add Models: the same choices as a "Connect your AI" card.
// * Settings > Integrations: "Connect your accounts" at the top (patch 0025):
//   mail and calendar, files and notes, work apps (the 0021 app cards).
// * Coming back from OpenRouter (?ow_connect=...) or from Sign in with
//   Google (?ow_accounts=..., patch 0025): a short note.
// * Use your ChatGPT plan (patch 0022): OpenAI's official Sign in with
//   ChatGPT (wings/chatgpt_plan.py), a "Continue with ChatGPT" button, the
//   first-use note, and "Using ChatGPT plan" beside the model picker.
// * window.__owUnofficialSignin tells admin.js and slashCommands.js whether
//   to offer the sign-ins that pose as another company's app.
//
// Back end: wings/connect_routes.py. Styling: upstream's own classes
// (admin-card, admin-btn-add, settings-input, intg-card ...), so every look
// styles these too; the few rules below only lay them out.
import uiModule from './ui.js?v=20260916largetoolscroll1';
import settingsModule from './settings.js?v=20260912writingstyle3';
// Same module instance as ui.js's (same URL): upstream's Escape arbiter
// closes the newest registered layer first, so a dialog here closes before
// the Settings window behind it.
import { registerEscapeLayer } from './escMenuStack.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Key pages, checked on each provider's own docs or official repos.
const FEATURED_KEY_PROVIDERS = [
  { id: 'anthropic', name: 'Anthropic (Claude)', url: 'https://api.anthropic.com', logo: 'anthropic', keyUrl: 'https://platform.claude.com/settings/keys' },
  { id: 'openai', name: 'OpenAI', url: 'https://api.openai.com/v1', logo: 'openai', keyUrl: 'https://platform.openai.com/settings/organization/api-keys' },
  { id: 'gemini', name: 'Google Gemini (AI Studio)', url: 'https://generativelanguage.googleapis.com/v1beta/openai', logo: 'gemini', keyUrl: 'https://aistudio.google.com/app/apikey' },
  { id: 'xai', name: 'xAI Grok', url: 'https://api.x.ai/v1', logo: 'grok', keyUrl: 'https://console.x.ai' },
  { id: 'mistral', name: 'Mistral', url: 'https://api.mistral.ai/v1', logo: 'mistral', keyUrl: 'https://console.mistral.ai/api-keys' },
  { id: 'groq', name: 'Groq', url: 'https://api.groq.com/openai/v1', logo: 'groq', keyUrl: 'https://console.groq.com/keys' },
  { id: 'deepseek', name: 'DeepSeek', url: 'https://api.deepseek.com/v1', logo: 'deepseek', keyUrl: 'https://platform.deepseek.com/api_keys' },
  { id: 'openrouter', name: 'OpenRouter', url: 'https://openrouter.ai/api/v1', logo: 'openrouter', keyUrl: 'https://openrouter.ai/settings/keys' },
  { id: 'huggingface', name: 'Hugging Face', url: 'https://router.huggingface.co/v1', logo: '', keyUrl: 'https://huggingface.co/settings/tokens/new?ownUserPermissions=inference.serverless.write&tokenType=fineGrained' },
];
// Same prefixes /setup recognises (slashCommands.js), plus Hugging Face.
const KEY_PREFIXES = [
  [/^sk-ant-/, 'https://api.anthropic.com'],
  [/^sk-or-/, 'https://openrouter.ai/api/v1'],
  [/^sk-proj-/, 'https://api.openai.com/v1'],
  [/^gsk_/, 'https://api.groq.com/openai/v1'],
  [/^AIza/, 'https://generativelanguage.googleapis.com/v1beta/openai'],
  [/^xai-/, 'https://api.x.ai/v1'],
  [/^nvapi-/, 'https://integrate.api.nvidia.com/v1'],
  [/^hf_/, 'https://router.huggingface.co/v1'],
];

const OPENROUTER_ERRORS = {
  denied: 'OpenRouter sign-in was cancelled. Nothing was changed.',
  expired: 'That sign-in ran out of time or was already used. Press Connect OpenRouter again.',
  other_user: 'That sign-in was started by another OpenWings account. Press Connect OpenRouter again.',
  exchange_failed: 'OpenRouter did not hand over a key. Please try again.',
  save_failed: 'OpenRouter signed you in, but the key could not be saved. Please try again.',
  not_admin: 'Only an OpenWings admin can add AI providers.',
  no_code: 'Paste the code OpenRouter showed you.',
};

const CHATGPT_ERRORS = {
  plan_off: 'You signed in, but ChatGPT plan use was not turned on, so OpenWings cannot use your plan. Turn it on in Settings > Add Models, or use an API key or OpenRouter.',
  denied: 'ChatGPT sign-in was cancelled. Nothing was changed. An API key or OpenRouter works too.',
  expired: 'That sign-in ran out of time or was already used. Press Continue with ChatGPT again.',
  other_user: 'That sign-in was started by another OpenWings account. Press Continue with ChatGPT again.',
  not_admin: 'Only an OpenWings admin can connect ChatGPT.',
  registration_incomplete: 'OpenAI did not finish registering OpenWings. Please try again.',
  client_mismatch: 'That sign-in came back for a different OpenWings registration, so nothing was saved. Please try again.',
  other_account: 'That was a different ChatGPT account from the one connected before, so nothing was saved. To switch, use "Use a different ChatGPT account" in Settings > Add Models.',
  already_connected: 'A ChatGPT account is already connected. Sign it out first.',
  bad_id_token: 'OpenAI\'s sign-in could not be verified, so nothing was saved. Please try again.',
  no_refresh_token: 'OpenAI did not allow OpenWings to stay signed in, so nothing was saved. Please try again.',
  invalid_client: 'OpenAI did not accept OpenWings\' registration. Use "Use a different ChatGPT account" in Settings > Add Models and sign in again.',
  temporary: 'OpenAI\'s sign-in service did not answer. Try again in a minute.',
};
const CHATGPT_USAGE_URL = 'https://chatgpt.com/settings/usage';
// The ChatGPT logo from OpenAI's approved Sign in with ChatGPT button assets
// (developers.openai.com/assets/siwc/sign-in-buttons/chatgpt-logo-white.svg),
// used as OpenAI's UI guidelines ask, unchanged.
const GPT_LOGO_PATH = 'M11.8102 19.1625C11.2327 19.1625 10.6841 19.0528 10.1643 18.8334C9.64459 18.6139 9.18259 18.3078 8.77834 17.9151C8.33944 18.0653 7.88322 18.1404 7.40967 18.1404C6.63582 18.1404 5.91972 17.9498 5.26137 17.5686C4.60302 17.1875 4.07172 16.6677 3.66747 16.0094C3.27477 15.351 3.07842 14.6176 3.07842 13.8091C3.07842 13.4742 3.12462 13.1103 3.21702 12.7176C2.75502 12.2903 2.39697 11.7994 2.14287 11.245C1.88877 10.679 1.76172 10.09 1.76172 9.47785C1.76172 8.85415 1.89454 8.25355 2.16019 7.67605C2.42584 7.09855 2.79544 6.6019 3.26899 6.1861C3.75409 5.75875 4.31427 5.46422 4.94952 5.30252C5.07657 4.64417 5.34222 4.05512 5.74647 3.53537C6.16227 3.00407 6.67047 2.58827 7.27107 2.28797C7.87167 1.98767 8.51269 1.83752 9.19414 1.83752C9.77164 1.83752 10.3203 1.94725 10.84 2.1667C11.3598 2.38615 11.8218 2.69222 12.226 3.08492C12.6649 2.93477 13.1211 2.8597 13.5947 2.8597C14.3685 2.8597 15.0846 3.05027 15.743 3.43142C16.4013 3.81257 16.9269 4.33232 17.3196 4.99067C17.7238 5.64902 17.9259 6.38245 17.9259 7.19095C17.9259 7.5259 17.8797 7.88973 17.7873 8.28242C18.2493 8.70977 18.6074 9.20643 18.8615 9.77238C19.1156 10.3268 19.2426 10.91 19.2426 11.5222C19.2426 12.1459 19.1098 12.7465 18.8442 13.324C18.5785 13.9015 18.2031 14.4039 17.718 14.8313C17.2445 15.2471 16.6901 15.5358 16.0548 15.6975C15.9278 16.3559 15.6564 16.9449 15.2406 17.4647C14.8363 17.996 14.3339 18.4118 13.7333 18.7121C13.1327 19.0124 12.4917 19.1625 11.8102 19.1625ZM7.53094 16.9969C8.10844 16.9969 8.61087 16.8756 9.03822 16.6331L12.2953 14.762C12.4108 14.6811 12.4686 14.5714 12.4686 14.4328V12.9429L8.27592 15.351C8.02182 15.5012 7.76772 15.5012 7.51362 15.351L4.23919 13.4626C4.23919 13.4973 4.23342 13.5377 4.22187 13.5839C4.22187 13.6301 4.22187 13.6994 4.22187 13.7918C4.22187 14.3808 4.36047 14.9237 4.63767 15.4203C4.92642 15.9054 5.32489 16.2866 5.83309 16.5638C6.34129 16.8525 6.90724 16.9969 7.53094 16.9969ZM7.70419 14.1729C7.77349 14.2076 7.83702 14.2249 7.89477 14.2249C7.95252 14.2249 8.01027 14.2076 8.06802 14.1729L9.36739 13.4279L5.19207 11.0024C4.93797 10.8523 4.81092 10.6271 4.81092 10.3268V6.56725C4.23342 6.82135 3.77142 7.21405 3.42492 7.74535C3.07842 8.2651 2.90517 8.8426 2.90517 9.47785C2.90517 10.0438 3.04954 10.5866 3.33829 11.1064C3.62704 11.6261 4.00242 12.0189 4.46442 12.2845L7.70419 14.1729ZM11.8102 18.0191C12.4224 18.0191 12.9768 17.8805 13.4734 17.6033C13.9701 17.3261 14.3628 16.9449 14.6515 16.4598C14.9403 15.9747 15.0846 15.4319 15.0846 14.8313V11.0891C15.0846 10.9505 15.0269 10.8465 14.9114 10.7772L13.5947 10.0149V14.8486C13.5947 15.1489 13.4676 15.3741 13.2135 15.5243L9.93912 17.4127C10.5051 17.817 11.1288 18.0191 11.8102 18.0191ZM12.4686 11.6781V9.32192L10.5108 8.21312L8.53579 9.32192V11.6781L10.5108 12.7869L12.4686 11.6781ZM7.40967 6.15145C7.40967 5.85115 7.53672 5.62592 7.79082 5.47577L11.0652 3.58735C10.4993 3.1831 9.87559 2.98097 9.19414 2.98097C8.58199 2.98097 8.02759 3.11957 7.53094 3.39677C7.03429 3.67397 6.64159 4.05512 6.35284 4.54022C6.07564 5.02532 5.93704 5.56817 5.93704 6.16878V9.89365C5.93704 10.0323 5.99479 10.142 6.11029 10.2228L7.40967 10.9851V6.15145ZM16.2108 14.4328C16.7883 14.1787 17.2445 13.786 17.5794 13.2547C17.9259 12.7234 18.0992 12.1459 18.0992 11.5222C18.0992 10.9562 17.9548 10.4134 17.6661 9.89365C17.3773 9.3739 17.0019 8.9812 16.5399 8.71555L13.3002 6.84445C13.2309 6.79825 13.1673 6.78092 13.1096 6.79248C13.0518 6.79248 12.9941 6.8098 12.9363 6.84445L11.637 7.5721L15.8296 10.0149C15.9567 10.0842 16.0491 10.1766 16.1068 10.2921C16.1761 10.3961 16.2108 10.5231 16.2108 10.6733V14.4328ZM12.7284 5.6317C12.9825 5.47 13.2366 5.47 13.4907 5.6317L16.7825 7.55477C16.7825 7.47393 16.7825 7.36997 16.7825 7.24292C16.7825 6.68852 16.6439 6.163 16.3667 5.66635C16.101 5.15815 15.7141 4.7539 15.2059 4.4536C14.7093 4.1533 14.1318 4.00315 13.4734 4.00315C12.8959 4.00315 12.3935 4.12442 11.9661 4.36697L8.70904 6.23808C8.59354 6.31892 8.53579 6.42865 8.53579 6.56725V8.0572L12.7284 5.6317Z';
const gptLogo = (size, fill) => `<svg class="ow-gpt-logo" width="${size}" height="${size}" viewBox="0 0 21 21" fill="none" aria-hidden="true"><path d="${GPT_LOGO_PATH}" fill="${fill}"/></svg>`;

let _status = null;
let _statusPromise = null;

function fetchStatus(force = false) {
  if (_statusPromise && !force) return _statusPromise;
  _statusPromise = (async () => {
    try {
      const r = await fetch('/api/wings/connect/status', { credentials: 'same-origin' });
      _status = r.ok ? await r.json() : { admin: false };
    } catch (_) {
      _status = { admin: false };
    }
    window.__owUnofficialSignin = _status.unofficial_signin === true;
    try { window.dispatchEvent(new CustomEvent('ow-connect-status', { detail: _status })); } catch (_) {}
    return _status;
  })();
  return _statusPromise;
}

function injectStyle() {
  if (document.getElementById('ow-connect-style')) return;
  const st = document.createElement('style');
  st.id = 'ow-connect-style';
  st.textContent = `
    /* Only while the welcome card is showing: once a chat starts upstream adds
       .hidden, and the card must neither show nor take clicks (some looks set
       display with !important, so say it as firmly here). */
    #welcome-screen.ow-has-setup:not(.hidden) { z-index: 3; pointer-events: auto; width: min(640px, calc(100% - 24px)); }
    html #welcome-screen.ow-has-setup.hidden { display: none !important; pointer-events: none !important; }
    #welcome-screen.ow-has-setup .welcome-sub, #welcome-screen.ow-has-setup .welcome-tip { display: none !important; }
    /* First run: the message box goes to the bottom so the choices get the
       height between the top bar and it (fitWelcome places the card). */
    @media (min-width: 769px) {
      html:root .chat-container.welcome-active.ow-first-run.ow-first-run .chat-input-bar { margin-bottom: calc(14px + var(--safe-bottom, 0px)) !important; }
    }
    .ow-setup-scroll { overflow-y: auto; overscroll-behavior: contain; }
    .ow-setup-scroll.ow-more { -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 40px), transparent); mask-image: linear-gradient(to bottom, #000 calc(100% - 40px), transparent); }
    .ow-more-cue { display: none; text-align: center; font-size: 11px; opacity: 0.75; margin-top: 2px; }
    .ow-setup-scroll.ow-more + .ow-more-cue { display: block; }
    .ow-member-note { margin-top: 10px; font-size: 13px; opacity: 0.8; text-align: center; }
    /* First run is a task screen: a compact title leaves room for the choices
       (doubled class so it outranks each look's own welcome title rule). */
    html #welcome-screen.ow-has-setup.ow-has-setup .welcome-name { flex-direction: row !important; justify-content: center !important; align-items: center !important; gap: 10px !important; font-size: 26px !important; margin: 0 !important; }
    html #welcome-screen.ow-has-setup.ow-has-setup .welcome-name .welcome-boat { width: 34px !important; height: 34px !important; border-radius: 10px !important; margin: 0 !important; }
    .ow-welcome-head { font-weight: 600; font-size: 15px; margin-bottom: 2px; }
    #welcome-setup { pointer-events: auto; text-align: left; width: 100%; margin-top: 12px; }
    .ow-connect { display: flex; flex-direction: column; gap: 6px; }
    .ow-connect .ow-lead { font-size: 12px; opacity: 0.75; margin-bottom: 2px; }
    .ow-connect .ow-choice { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: color-mix(in srgb, var(--fg) 3%, transparent); }
    .ow-connect .ow-choice-row { display: flex; align-items: center; gap: 10px; }
    .ow-connect .ow-choice-text { flex: 1; min-width: 0; }
    .ow-connect .ow-choice-title { font-weight: 600; font-size: 13px; }
    .ow-connect .ow-choice-sub { font-size: 11.5px; opacity: 0.7; line-height: 1.4; margin-top: 1px; }
    .ow-connect .ow-choice-act { flex-shrink: 0; display: flex; gap: 6px; align-items: center; }
    .ow-connect .ow-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
    .ow-connect .ow-row > .settings-input, .ow-connect .ow-row > .settings-select { flex: 1 1 180px; min-width: 0; }
    .ow-connect .ow-found { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 12px; }
    .ow-connect .ow-found span { flex: 1; min-width: 0; }
    .ow-connect .ow-link { font-size: 12px; white-space: nowrap; }
    /* Links in these panels take the look's accent (not the browser's blue). */
    .ow-connect a:not(.ow-gpt-btn):not(.admin-btn-add), .ow-apps-grid a:not(.admin-btn-add) { color: var(--accent, var(--red)); text-decoration: underline; text-underline-offset: 2px; }
    .ow-connect .ow-key-help { font-size: 11.5px; opacity: 0.85; margin-top: 4px; }
    @media (max-width: 560px) {
      .ow-connect .ow-choice-row { flex-direction: column; align-items: stretch; gap: 6px; }
      .ow-connect .ow-choice-act { justify-content: flex-start; flex-wrap: wrap; }
      .ow-connect .ow-found { flex-wrap: wrap; }
    }
    .ow-connect .adm-ep-inline-msg:empty { display: none; }
    .ow-connect .ow-sub-panel ul { margin: 6px 0 0 18px; padding: 0; font-size: 12px; line-height: 1.5; }
    .ow-connect .ow-sub-panel li { margin-bottom: 5px; }
    .ow-connect .ow-choice-quiet { background: none; border-style: dashed; }
    .ow-connect button.ow-plain { background: none; border: 0; padding: 0; color: inherit; font: inherit; font-size: 12px; cursor: pointer; text-decoration: underline; text-underline-offset: 2px; text-align: left; }
    .ow-apps-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
    .ow-apps-grid .intg-card { display: flex; flex-direction: column; align-items: stretch; cursor: default; margin-bottom: 0; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: color-mix(in srgb, var(--fg) 3%, transparent); }
    .ow-card-ico { vertical-align: -2px; margin-right: 5px; opacity: 0.6; }
    .ow-app-head { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; }
    .ow-app-about { font-size: 11px; opacity: 0.65; margin-top: 2px; }
    .ow-app-state { font-size: 11px; margin-top: 6px; display: flex; align-items: center; gap: 5px; }
    .ow-app-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--fg); opacity: 0.3; flex-shrink: 0; }
    .ow-app-dot.on { background: var(--color-success, #50fa7b); opacity: 1; }
    .ow-app-dot.warn { background: var(--color-warning, #e5a33a); opacity: 1; }
    .ow-app-actions { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
    .ow-app-note { font-size: 10.5px; opacity: 0.6; margin-top: 6px; line-height: 1.4; }
    .ow-app-error { font-size: 11px; opacity: 0.75; margin-top: 3px; line-height: 1.4; }
    /* OpenAI's button format: "Continue with ChatGPT" on black, with the logo. */
    .ow-gpt-btn { display: inline-flex; align-items: center; gap: 7px; background: #000; color: #fff !important; border: 1px solid rgba(255,255,255,0.22); border-radius: 999px; padding: 6px 13px 6px 10px; font: inherit; font-size: 12.5px; font-weight: 600; line-height: 1.2; cursor: pointer; text-decoration: none !important; white-space: nowrap; }
    .ow-gpt-btn:hover { background: #1f1f1f; }
    .ow-gpt-btn:disabled { opacity: 0.6; cursor: default; }
    .ow-gpt-btn .ow-gpt-logo { flex-shrink: 0; }
    .ow-connect .ow-gpt-steps { margin: 6px 0 0 18px; padding: 0; font-size: 12px; line-height: 1.5; }
    .ow-connect .ow-gpt-steps li { margin-bottom: 6px; }
    .ow-connect .ow-gpt-steps code, .ow-connect .ow-gpt-note code { display: block; margin-top: 3px; padding: 4px 6px; border-radius: 6px; background: color-mix(in srgb, var(--fg) 7%, transparent); font-size: 11px; white-space: pre-wrap; word-break: break-all; user-select: all; }
    .ow-connect .ow-gpt-note { font-size: 11.5px; opacity: 0.8; margin-top: 6px; line-height: 1.45; }
    .ow-gpt-chip { display: flex; justify-content: flex-end; align-items: center; gap: 5px; font-size: 10.5px; line-height: 1.2; opacity: 0.85; margin-top: 2px; white-space: nowrap; }
    .ow-gpt-chip[hidden] { display: none; }
    .ow-gpt-chip a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
    .ow-gpt-chip .ow-gpt-mark { display: inline-flex; width: 14px; height: 14px; border-radius: 50%; background: #000; align-items: center; justify-content: center; }
    .ow-connect .ow-choice.ow-flash { outline: 2px solid var(--accent, var(--red)); outline-offset: 1px; }
    /* Connect your accounts (patch 0025). */
    .ow-acct-group { margin-top: 10px; }
    .ow-acct-group:first-child { margin-top: 0; }
    .ow-acct-group-title { font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--fg); margin: 0 0 6px 2px; }
    .ow-apps-grid .intg-card.ow-flash { outline: 2px solid var(--accent, var(--red)); outline-offset: 1px; }
    .ow-feed-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
    .ow-feed-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 11.5px; }
    .ow-feed-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
    .ow-feed-name { flex: 1 1 120px; min-width: 0; overflow-wrap: anywhere; font-weight: 600; }
    .ow-feed-sub { display: block; font-weight: 400; opacity: 0.65; font-size: 10.5px; }
    .ow-acct-dialog .modal-content { max-height: calc(100vh - 24px); display: flex; flex-direction: column; }
    .ow-acct-body { overflow-y: auto; display: flex; flex-direction: column; gap: 8px; font-size: 13px; line-height: 1.45; }
    .ow-acct-body a, .ow-apps-grid a:not(.admin-btn-add) { color: var(--accent, var(--red)); text-decoration: underline; text-underline-offset: 2px; opacity: 1; }
    .ow-acct-lead { opacity: 0.9; }
    .ow-acct-steps { margin: 0 0 0 20px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    .ow-acct-wizard li { line-height: 1.5; }
    .ow-acct-label { font-size: 12px; font-weight: 600; margin-bottom: -4px; }
    .ow-acct-body .settings-input { width: 100%; box-sizing: border-box; }
    .ow-acct-check { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; cursor: pointer; }
    .ow-acct-check input { margin-top: 3px; flex-shrink: 0; }
    .ow-acct-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 2px; }
    .ow-acct-warn { font-size: 12px; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border); border-left: 3px solid var(--accent, var(--red)); background: color-mix(in srgb, var(--fg) 4%, transparent); }
    .ow-acct-or { font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--fg); text-align: center; margin: 4px 0; }
    .ow-acct-choice { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
    .ow-acct-details summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--fg); opacity: 1; }
    .ow-acct-details > div { margin-top: 6px; font-size: 13px; }
    .ow-acct-dl { margin: 6px 0 0; font-size: 13px; }
    .ow-acct-dl dt { font-weight: 600; margin-top: 6px; }
    .ow-acct-dl dd { margin: 2px 0 0; font-size: 13px; }
    .ow-acct-dl dd a { font-size: 13px; }
    .ow-acct-notpossible { font-size: 11.5px; line-height: 1.45; margin-top: 10px; color: var(--fg); }
    .ow-acct-copy { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin: 6px 0; }
    .ow-acct-copy code { flex: 1 1 200px; min-width: 0; padding: 5px 7px; border-radius: 6px; background: color-mix(in srgb, var(--fg) 7%, transparent); font-size: 11.5px; word-break: break-all; user-select: all; }
    .ow-acct-body .adm-ep-inline-msg:empty, .ow-apps-grid .adm-ep-inline-msg:empty { display: none; }
    @media (max-width: 560px) {
      .ow-apps-grid { grid-template-columns: 1fr; }
      .ow-acct-dialog .modal-content { width: calc(100vw - 16px) !important; }
    }
    #ow-gpt-welcome .modal-content { max-width: 420px; }
    #ow-gpt-welcome .ow-gpt-welcome-logo { display: flex; justify-content: center; margin: 4px 0 10px; }
    #ow-gpt-welcome .ow-gpt-welcome-logo span { display: inline-flex; width: 44px; height: 44px; border-radius: 50%; background: #000; align-items: center; justify-content: center; }
  `;
  document.head.appendChild(st);
}

function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || '';
  el.className = 'adm-ep-inline-msg' + (kind === 'ok' ? ' admin-success' : kind === 'err' ? ' admin-error' : '');
}

// ------------------------------------------------------------ AI panel

function keyProviders() {
  const out = FEATURED_KEY_PROVIDERS.slice();
  const seen = new Set(out.map((p) => p.url));
  // Reuse the other presets from Settings > Add Models (same base URLs).
  const sel = document.getElementById('adm-epProvider');
  if (sel) {
    for (const o of Array.from(sel.options)) {
      if (!o.value || (o.dataset && o.dataset.authFlow) || seen.has(o.value)) continue;
      out.push({ id: o.value, name: o.textContent.trim(), url: o.value, logo: (o.dataset && o.dataset.logo) || '', keyUrl: '' });
      seen.add(o.value);
    }
  }
  return out;
}

function aiPanelHtml(where) {
  const providers = keyProviders();
  const options = providers.map((p) => `<option value="${esc(p.url)}">${esc(p.name)}</option>`).join('');
  const lead = where === 'welcome'
    ? 'OpenWings needs an AI to talk to. Pick any one; you can add more later.'
    : 'The quickest ways to add an AI. The cards below are for anything else.';
  const keyOpen = where !== 'welcome';
  return `
    <div class="ow-connect" data-ow-where="${esc(where)}">
      <div class="ow-lead">${lead}</div>
      <div class="ow-choice" data-ow-choice="local">
        <div class="ow-choice-row">
          <div class="ow-choice-text">
            <div class="ow-choice-title">Models already on this computer</div>
            <div class="ow-choice-sub" data-ow-local-sub>Ollama or LM Studio: free and private.</div>
          </div>
          <div class="ow-choice-act"><button type="button" class="admin-btn-sm" data-ow-act="local-scan">Look for them</button></div>
        </div>
        <div data-ow-local></div>
      </div>
      <div class="ow-choice" data-ow-choice="openrouter">
        <div class="ow-choice-row">
          <div class="ow-choice-text">
            <div class="ow-choice-title">Connect OpenRouter</div>
            <div class="ow-choice-sub">One sign-in for Claude, GPT, Gemini, Llama and more, paid from your own OpenRouter credits. Everyone on this OpenWings can use it; you can set a <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener noreferrer">spending limit</a> for its key on OpenRouter.</div>
          </div>
          <div class="ow-choice-act"><button type="button" class="admin-btn-add" data-ow-act="openrouter">Connect</button></div>
        </div>
        <div data-ow-or-paste hidden>
          <div class="ow-choice-sub" style="margin-top:6px;">After you approve, OpenRouter shows a code. Paste it here.</div>
          <div class="ow-row">
            <input type="text" class="settings-input" data-ow-or-code placeholder="Code from OpenRouter" autocomplete="off" aria-label="Code from OpenRouter">
            <button type="button" class="admin-btn-add" data-ow-act="openrouter-finish">Finish</button>
          </div>
        </div>
        <div class="adm-ep-inline-msg" data-ow-or-msg></div>
      </div>
      <div class="ow-choice" data-ow-choice="chatgpt">
        <div class="ow-choice-row">
          <div class="ow-choice-text">
            <div class="ow-choice-title">Use your ChatGPT plan</div>
            <div class="ow-choice-sub" data-ow-gpt-sub>ChatGPT Plus or Pro: OpenWings' AI requests use your plan's usage, through OpenAI's own sign-in. No API key or extra charges from OpenWings.</div>
          </div>
          <div class="ow-choice-act" data-ow-gpt-act></div>
        </div>
        <div data-ow-gpt-more></div>
        <div class="adm-ep-inline-msg" data-ow-gpt-msg></div>
      </div>
      <div class="ow-choice" data-ow-choice="key">
        <div class="ow-choice-row">
          <div class="ow-choice-text">
            <div class="ow-choice-title">Paste a key</div>
            <div class="ow-choice-sub">Anthropic, OpenAI, Google Gemini, xAI, Mistral, Groq, DeepSeek, Hugging Face and more.</div>
          </div>
          <div class="ow-choice-act"><button type="button" class="admin-btn-sm" data-ow-act="toggle" data-ow-target="key" aria-expanded="${keyOpen}">${keyOpen ? 'Hide' : 'Paste a key'}</button></div>
        </div>
        <div data-ow-panel="key"${keyOpen ? '' : ' hidden'}>
          <div class="ow-row">
            <select class="settings-select" data-ow-key-provider aria-label="Provider">${options}</select>
            <a class="ow-link" data-ow-key-link href="#" target="_blank" rel="noopener noreferrer">Get a key</a>
          </div>
          <div class="ow-row">
            <input type="password" class="settings-input" data-ow-key-input placeholder="Paste your API key" autocomplete="off" aria-label="API key">
            <button type="button" class="admin-btn-sm" data-ow-act="key-test">Test</button>
            <button type="button" class="admin-btn-add" data-ow-act="key-add">Add</button>
          </div>
          <div class="ow-key-help">Everyone on this OpenWings can use an AI you add here.</div>
          <div class="adm-ep-inline-msg" data-ow-key-msg></div>
        </div>
      </div>
      <div class="ow-choice ow-choice-quiet" data-ow-choice="subscription">
        <button type="button" class="ow-plain" data-ow-act="toggle" data-ow-target="sub" aria-expanded="false">I have a Claude, ChatGPT or Gemini subscription</button>
        <div class="ow-sub-panel" data-ow-panel="sub" hidden>
          <div class="ow-choice-sub" style="margin-top:6px;"><strong>ChatGPT Plus or Pro:</strong> press Continue with ChatGPT under Use your ChatGPT plan above. It is OpenAI's own sign-in, so OpenWings never sees your password. <button type="button" class="ow-plain" data-ow-act="gpt-focus">Show me</button></div>
          <div class="ow-choice-sub" style="margin-top:6px;">Claude and Gemini plans only work in Anthropic's and Google's own apps: their rules don't let other apps use them, so OpenWings never asks for your plan login. What works instead:</div>
          <ul>
            <li><strong>An API key</strong> from the same company, paid as you go: <a href="https://platform.claude.com/settings/keys" target="_blank" rel="noopener noreferrer">Claude key</a>, <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener noreferrer">Gemini key</a> (has a free tier), <a href="https://platform.openai.com/settings/organization/api-keys" target="_blank" rel="noopener noreferrer">OpenAI key</a>. Then use Paste a key.</li>
            <li><strong>OpenRouter</strong>: one sign-in for Claude, GPT, Gemini and more. <button type="button" class="ow-plain" data-ow-act="openrouter">Connect OpenRouter</button></li>
            <li><strong>Keep using your Claude plan</strong> and let your own Claude Code use OpenWings: Settings &gt; Integrations &gt; Add Integration &gt; Claude Agent. <button type="button" class="ow-plain" data-ow-act="claude-agent">Open it</button> (Codex users: Codex Agent, same place.)</li>
          </ul>
        </div>
      </div>
    </div>`;
}

const OLLAMA_HOST_HELP = 'https://docs.ollama.com/faq#how-do-i-configure-ollama-server';

function localLabel(item) {
  const port = Number(item.port);
  let kind = 'Model server';
  if (item.provider === 'lmstudio' || port === 1234) kind = 'LM Studio';
  else if (item.provider === 'llamacpp') kind = 'llama.cpp';
  else if (port === 11434) kind = 'Ollama';
  const here = ['host.docker.internal', 'localhost', '127.0.0.1'].includes(String(item.host || ''));
  const host = here ? 'this computer' : `${item.host}:${port}`;
  return { kind, text: `${kind} on ${host}` };
}

// Make sure a result message is on screen (the first-run panel scrolls).
function showMsg(el) {
  try { el && el.scrollIntoView({ block: 'nearest' }); } catch (_) {}
}

async function scanLocal(root, onAdded) {
  const box = root.querySelector('[data-ow-local]');
  const sub = root.querySelector('[data-ow-local-sub]');
  const btn = root.querySelector('[data-ow-act="local-scan"]');
  if (!box) return;
  box.innerHTML = '';
  if (sub) sub.textContent = 'Looking for Ollama and LM Studio…';
  if (btn) btn.disabled = true;
  let items = [];
  try {
    const r = await fetch('/api/discover', { credentials: 'same-origin' });
    const d = r.ok ? await r.json() : {};
    items = d.items || [];
  } catch (_) { items = []; }
  if (btn) { btn.disabled = false; btn.textContent = 'Look again'; }
  if (!items.length) {
    if (sub) sub.innerHTML = `None found. Using Ollama? Start it with <code>OLLAMA_HOST=0.0.0.0</code> so OpenWings can reach it (<a href="${OLLAMA_HOST_HELP}" target="_blank" rel="noopener noreferrer">How?</a>). Other devices on your network can then reach Ollama too.`;
    refit();
    return;
  }
  if (sub) sub.textContent = items.length === 1 ? 'Found one. Add it:' : `Found ${items.length}. Add one:`;
  box.innerHTML = items.map((it, i) => {
    const lab = localLabel(it);
    const n = (it.models || []).length;
    return `<div class="ow-found"><span>${esc(lab.text)} · ${n} model${n === 1 ? '' : 's'}</span><button type="button" class="admin-btn-add" data-ow-add-local="${i}">Add</button></div>`;
  }).join('') + '<div class="adm-ep-inline-msg" data-ow-local-msg></div>';
  refit();
  box.querySelectorAll('[data-ow-add-local]').forEach((b) => {
    b.addEventListener('click', async () => {
      const it = items[Number(b.dataset.owAddLocal)];
      const msg = box.querySelector('[data-ow-local-msg]');
      const base = String(it.url || '').replace('/chat/completions', '').replace(/\/$/, '');
      const fd = new FormData();
      fd.append('base_url', base);
      fd.append('name', localLabel(it).text);
      fd.append('endpoint_kind', 'local');
      fd.append('model_refresh_mode', 'auto');
      fd.append('skip_probe', 'false');
      b.disabled = true;
      setMsg(msg, 'Adding…');
      try {
        const r = await fetch('/api/model-endpoints', { method: 'POST', body: fd, credentials: 'same-origin' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
        setMsg(msg, `Added ${localLabel(it).kind}. You're ready to chat.`, 'ok');
        showMsg(msg);
        onAdded && onAdded(msg);
      } catch (e) {
        setMsg(msg, `Could not add it: ${e.message || 'request failed'}`, 'err');
        b.disabled = false;
      }
    });
  });
}

function selectedKeyProvider(root) {
  const sel = root.querySelector('[data-ow-key-provider]');
  const url = sel ? sel.value : '';
  return keyProviders().find((p) => p.url === url) || null;
}

function syncKeyLink(root) {
  const p = selectedKeyProvider(root);
  const link = root.querySelector('[data-ow-key-link]');
  if (!link) return;
  if (p && p.keyUrl) {
    link.href = p.keyUrl;
    link.hidden = false;
    link.textContent = `Get your ${p.name.replace(/ \(.*\)$/, '')} key`;
  } else {
    link.hidden = true;
    link.removeAttribute('href');
  }
}

function keyForm(root) {
  const p = selectedKeyProvider(root);
  const key = (root.querySelector('[data-ow-key-input]')?.value || '').trim();
  return { p, key };
}

function shortName(p) { return p ? p.name.replace(/ \(.*\)$/, '') : 'The provider'; }

// Upstream's raw errors ("HTTP 404", "host does not resolve: [Errno -2] ...")
// in words a new user can act on, with the key page where there is one.
function keyErrorHtml(p, raw, status) {
  const name = esc(shortName(p));
  const text = String(raw || '');
  const link = p && p.keyUrl ? ` <a href="${esc(p.keyUrl)}" target="_blank" rel="noopener noreferrer">Get your ${name} key</a>` : '';
  if (status === 403 && /switched off/i.test(text)) return esc(text);
  if (/does not resolve|name or service not known|getaddrinfo|nodename nor servname|temporary failure in name resolution|connecterror|connection refused|network is unreachable|could not connect|cannot reach|timed? ?out|timeout/i.test(text)) {
    return `OpenWings could not reach ${name}. Check this computer's internet connection, then try again.`;
  }
  if (/\b(HTTP )?(400|401|402|403|404)\b|invalid|unauthori[sz]ed|forbidden|api[ _-]?key|no models found/i.test(text) || [400, 401, 403, 404].includes(status)) {
    return `${name} did not accept that key. Check you copied all of it, or get a new one.${link}`;
  }
  return `${name} did not answer with any models. Check the key, or try again in a minute.${link}`;
}

function setMsgHtml(el, html, kind) {
  if (!el) return;
  el.innerHTML = html;
  el.className = 'adm-ep-inline-msg' + (kind === 'ok' ? ' admin-success' : kind === 'err' ? ' admin-error' : '');
}

async function testKey(root) {
  const msg = root.querySelector('[data-ow-key-msg]');
  const { p, key } = keyForm(root);
  if (!p) return setMsg(msg, 'Pick a provider first.', 'err');
  if (!key) return setMsg(msg, 'Paste your API key first.', 'err');
  setMsg(msg, `Checking the key with ${shortName(p)}…`);
  const fd = new FormData();
  fd.append('base_url', p.url);
  fd.append('api_key', key);
  fd.append('endpoint_kind', 'api');
  fd.append('model_refresh_timeout', '30');
  try {
    const r = await fetch('/api/model-endpoints/test', { method: 'POST', body: fd, credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    const n = (d.models || []).length;
    if (r.ok && d.online && n) {
      setMsg(msg, `It works: ${n} model${n === 1 ? '' : 's'} available.`, 'ok');
    } else {
      setMsgHtml(msg, keyErrorHtml(p, d.detail || d.ping_error || '', r.ok ? 0 : r.status), 'err');
    }
  } catch (e) {
    setMsgHtml(msg, keyErrorHtml(p, e.message || 'could not connect', 0), 'err');
  }
  showMsg(msg);
}

async function addKey(root, onAdded) {
  const msg = root.querySelector('[data-ow-key-msg]');
  const { p, key } = keyForm(root);
  if (!p) return setMsg(msg, 'Pick a provider first.', 'err');
  if (!key) return setMsg(msg, 'Paste your API key first.', 'err');
  const btn = root.querySelector('[data-ow-act="key-add"]');
  if (btn) btn.disabled = true;
  setMsg(msg, `Checking the key with ${shortName(p)}…`);
  const fd = new FormData();
  fd.append('base_url', p.url);
  fd.append('api_key', key);
  fd.append('name', shortName(p));
  fd.append('endpoint_kind', 'api');
  if (!/generativelanguage\.googleapis\.com/.test(p.url)) fd.append('model_refresh_mode', 'auto');
  fd.append('model_refresh_timeout', '30');
  // Only keep an endpoint the provider actually answered with models for:
  // the server refuses (400) otherwise, so a mistyped key leaves nothing.
  fd.append('require_models', 'true');
  try {
    const r = await fetch('/api/model-endpoints', { method: 'POST', body: fd, credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    const n = (d.models || []).length;
    if (!r.ok || !n) {
      setMsgHtml(msg, keyErrorHtml(p, d.detail || 'no models found', r.ok ? 0 : r.status), 'err');
      showMsg(msg);
      return;
    }
    const input = root.querySelector('[data-ow-key-input]');
    if (input) input.value = '';
    setMsg(msg, `Added ${shortName(p)} with ${n} model${n === 1 ? '' : 's'}. You're ready to chat.`, 'ok');
    showMsg(msg);
    onAdded && onAdded(msg);
  } catch (e) {
    setMsgHtml(msg, keyErrorHtml(p, e.message || 'could not connect', 0), 'err');
    showMsg(msg);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function connectOpenRouter(root) {
  const msg = root.querySelector('[data-ow-or-msg]');
  const buttons = root.querySelectorAll('[data-ow-act="openrouter"]');
  buttons.forEach((b) => { b.disabled = true; });
  setMsg(msg, 'Opening OpenRouter…');
  try {
    const r = await fetch('/api/wings/connect/openrouter/start', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const url = String(d.authorize_url || '');
    if (!url.startsWith('https://openrouter.ai/')) throw new Error('unexpected sign-in address');
    if (d.mode === 'callback') {
      // OpenRouter sends the browser back here; the server finishes.
      window.location.assign(url);
      return;
    }
    // Plain-http address other than localhost: OpenRouter shows a code to paste.
    root._owOrFlow = d.flow;
    const paste = root.querySelector('[data-ow-or-paste]');
    if (paste) paste.hidden = false;
    msg.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open OpenRouter</a>, approve, then paste the code below.`;
    msg.className = 'adm-ep-inline-msg';
    window.open(url, '_blank', 'noopener');
  } catch (e) {
    setMsg(msg, 'Could not start: ' + (e.message || 'request failed'), 'err');
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function finishOpenRouter(root, onAdded) {
  const msg = root.querySelector('[data-ow-or-msg]');
  const code = (root.querySelector('[data-ow-or-code]')?.value || '').trim();
  if (!code) return setMsg(msg, OPENROUTER_ERRORS.no_code, 'err');
  setMsg(msg, 'Finishing…');
  try {
    const r = await fetch('/api/wings/connect/openrouter/code', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flow: root._owOrFlow || '', code }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) {
      // The code was used up either way: ask for a fresh one instead of
      // leaving a box that can only fail again.
      const err = new Error(d.error === 'exchange_failed' || d.error === 'expired'
        ? 'That code did not work. Press Connect again for a new code.'
        : (OPENROUTER_ERRORS[d.error] || d.detail || 'Could not finish. Press Connect again.'));
      if (d.error !== 'no_code') err.restart = true;
      throw err;
    }
    setMsg(msg, 'OpenRouter is connected. Everyone on this OpenWings can use it.', 'ok');
    const paste = root.querySelector('[data-ow-or-paste]');
    if (paste) paste.hidden = true;
    showMsg(msg);
    onAdded && onAdded(msg);
  } catch (e) {
    if (e.restart) {
      const paste = root.querySelector('[data-ow-or-paste]');
      if (paste) paste.hidden = true;
      const codeInput = root.querySelector('[data-ow-or-code]');
      if (codeInput) codeInput.value = '';
      root._owOrFlow = '';
    }
    setMsg(msg, e.message, 'err');
    showMsg(msg);
  }
}

// ------------------------------------------------------------ ChatGPT plan

function gptStatus() { return (_status && _status.chatgpt) || { mode: 'remote', state: 'none' }; }

function gptButton(extra = '') {
  return `<button type="button" class="ow-gpt-btn" data-oai-siwc-button data-ow-act="gpt-start" ${extra}>${gptLogo(16, '#fff')}<span>Continue with ChatGPT</span></button>`;
}

function renderChatgpt(root) {
  const box = root.querySelector('[data-ow-choice="chatgpt"]');
  if (!box) return;
  const g = gptStatus();
  const sub = box.querySelector('[data-ow-gpt-sub]');
  const act = box.querySelector('[data-ow-gpt-act]');
  const more = box.querySelector('[data-ow-gpt-more]');
  const who = g.email ? ` as ${esc(g.email)}` : '';
  const manage = `<a href="${CHATGPT_USAGE_URL}" target="_blank" rel="noopener noreferrer">Manage usage</a>`;
  let actions = '';
  let notes = '';
  if (g.state === 'connected') {
    if (sub) sub.innerHTML = `Connected${who} · ${g.models || 0} model${g.models === 1 ? '' : 's'} · Using ChatGPT plan · ${manage}`;
    actions = (g.models ? '' : '<button type="button" class="admin-btn-sm" data-ow-act="gpt-models">Load models</button>')
      + '<button type="button" class="admin-btn-sm" data-ow-act="gpt-signout">Sign out</button>';
    if (g.transferred) notes += '<div class="ow-gpt-note">This sign-in was moved here from another computer. OpenAI does not yet track usage per server or end moved sign-ins one by one; to end it for sure, disconnect OpenWings in ChatGPT settings.</div>';
  } else if (g.state === 'plan_off') {
    if (sub) sub.innerHTML = `Signed in${who}, but ChatGPT plan use is off, so OpenWings cannot use your plan. To turn it on, Continue with ChatGPT and allow plan use. ${manage}`;
    actions = (g.mode === 'ready' ? gptButton('data-ow-consent="1"') : '')
      + '<button type="button" class="admin-btn-sm" data-ow-act="gpt-signout">Sign out</button>';
  } else {
    const intro = 'ChatGPT Plus or Pro: OpenWings\' AI requests use your plan\'s usage, through OpenAI\'s own sign-in. No API key or extra charges from OpenWings.';
    if (sub) {
      if (g.state === 'needs_signin') sub.textContent = `Your ChatGPT sign-in${g.email ? ` (${g.email})` : ''} ended. Continue with ChatGPT to sign in again.`;
      else if (g.state === 'signed_out' && g.email) sub.textContent = `Signed out (${g.email}). ${intro}`;
      else sub.textContent = intro;
    }
    if (g.mode === 'ready') actions = gptButton();
    else if (g.mode === 'switch' && /^http:\/\/127\.0\.0\.1:\d+\//.test(g.switch_url || '')) {
      actions = `<a class="ow-gpt-btn" data-oai-siwc-button href="${esc(g.switch_url)}">${gptLogo(16, '#fff')}<span>Continue with ChatGPT</span></a>`;
      notes += '<div class="ow-gpt-note">OpenAI sends the sign-in back to <strong>127.0.0.1</strong>, so this opens OpenWings at that address first (the same computer; you may need to sign in to OpenWings once more there). Then press Continue with ChatGPT again.</div>';
    } else if (g.mode === 'remote') {
      const port = Number(g.server_port) || 7011;
      const user = g.user || 'YOUR-OPENWINGS-NAME';
      notes += `<div class="ow-gpt-note">This OpenWings runs on another computer. OpenAI only sends the ChatGPT sign-in back to <strong>127.0.0.1</strong>, the computer you sign in on, so use one of these:</div>
        <ol class="ow-gpt-steps">
          <li><strong>Open OpenWings through an SSH tunnel</strong>, then press Continue with ChatGPT there. On your own computer run:<code>ssh -N -L ${port}:127.0.0.1:${port} you@your-server</code>then open <code>http://127.0.0.1:${port}</code></li>
          <li><strong>Or OpenAI's steps for self-hosted servers:</strong> press Continue with ChatGPT in OpenWings on your own computer, then move that sign-in here over SSH, from the OpenWings folder on your computer (use your OpenWings user name on each side; it stops working on your computer and this server takes over):<code>cd ~/openwings &amp;&amp; docker compose exec -T openwings python -m wings.chatgpt_plan export --user ${esc(user)} | ssh you@your-server 'cd ~/openwings &amp;&amp; docker compose exec -T openwings python -m wings.chatgpt_plan import --user ${esc(user)}'</code>OpenAI does not yet track usage per server or end moved sign-ins one by one.</li>
        </ol>`;
    }
    if (g.registered && (g.state === 'signed_out' || g.state === 'needs_signin')) {
      notes += '<div class="ow-gpt-note"><button type="button" class="ow-plain" data-ow-act="gpt-forget">Use a different ChatGPT account</button></div>';
    }
  }
  if (g.revoke_unconfirmed) {
    notes += `<div class="ow-gpt-note">OpenWings could not confirm with OpenAI that its access was removed. To be sure, disconnect OpenWings in <a href="${CHATGPT_USAGE_URL}" target="_blank" rel="noopener noreferrer">ChatGPT settings</a>.</div>`;
  }
  if (act) act.innerHTML = actions;
  if (more) more.innerHTML = notes;
  refit();
}

async function refreshChatgptPanels() {
  await fetchStatus(true);
  document.querySelectorAll('.ow-connect').forEach((r) => renderChatgpt(r));
  updatePlanChip();
}

async function startChatgpt(root, { consent = false } = {}) {
  const msg = root.querySelector('[data-ow-gpt-msg]');
  const btns = root.querySelectorAll('[data-ow-act="gpt-start"]');
  btns.forEach((b) => { b.disabled = true; });
  setMsg(msg, 'Opening ChatGPT…');
  try {
    const r = await fetch('/api/wings/connect/chatgpt/start', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ consent }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.status === 409 && d.mode) {
      if (_status) _status.chatgpt = Object.assign({}, _status.chatgpt || {}, d);
      renderChatgpt(root);
      setMsg(msg, '');
      return;
    }
    if (!r.ok || !d.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    const url = String(d.authorize_url || '');
    if (!url.startsWith('https://auth.openai.com/api/accounts/authorize?')) throw new Error('unexpected sign-in address');
    // A top-level visit: OpenAI sends this browser back to 127.0.0.1 here.
    window.location.assign(url);
  } catch (e) {
    setMsg(msg, 'Could not start: ' + (e.message || 'request failed'), 'err');
    btns.forEach((b) => { b.disabled = false; });
  }
}

async function signOutChatgpt(root) {
  const ok = window.styledConfirm
    ? await window.styledConfirm('Sign out of ChatGPT in OpenWings? OpenWings stops using your plan and asks OpenAI to end its access. You can sign in again any time.', { confirmText: 'Sign out', title: 'ChatGPT plan' })
    : window.confirm('Sign out of ChatGPT in OpenWings?');
  if (!ok) return;
  const msg = root.querySelector('[data-ow-gpt-msg]');
  setMsg(msg, 'Signing out…');
  try {
    const r = await fetch('/api/wings/connect/chatgpt/disconnect', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    await refreshChatgptPanels();
    setMsg(root.querySelector('[data-ow-gpt-msg]'), d.revoked ? 'Signed out. OpenAI ended OpenWings\' access.' : 'Signed out here.', d.revoked ? 'ok' : '');
    try { settingsModule.refreshAiModelEndpoints && settingsModule.refreshAiModelEndpoints(); } catch (_) {}
  } catch (e) {
    setMsg(msg, 'Could not sign out: ' + (e.message || 'request failed'), 'err');
  }
}

async function forgetChatgpt(root) {
  const ok = window.styledConfirm
    ? await window.styledConfirm('Use a different ChatGPT account? OpenWings forgets the account it knew; the next Continue with ChatGPT registers OpenWings with the account you pick.', { confirmText: 'Use a different account', title: 'ChatGPT plan' })
    : window.confirm('Use a different ChatGPT account?');
  if (!ok) return;
  const msg = root.querySelector('[data-ow-gpt-msg]');
  try {
    const r = await fetch('/api/wings/connect/chatgpt/forget', { method: 'POST', credentials: 'same-origin' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await refreshChatgptPanels();
  } catch (e) {
    setMsg(msg, 'Could not do that: ' + (e.message || 'request failed'), 'err');
  }
}

async function reloadChatgptModels(root, onAdded) {
  const msg = root.querySelector('[data-ow-gpt-msg]');
  setMsg(msg, 'Loading models…');
  try {
    const r = await fetch('/api/wings/connect/chatgpt/models', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.message || d.detail || `HTTP ${r.status}`);
    await refreshChatgptPanels();
    setMsg(root.querySelector('[data-ow-gpt-msg]'), `${d.models} model${d.models === 1 ? '' : 's'} ready.`, 'ok');
    onAdded && onAdded();
  } catch (e) {
    setMsg(msg, e.message || 'Could not load models.', 'err');
  }
}

function showChatgptWelcome() {
  // OpenAI's UI guidelines: once, after the first sign-in with plan use on.
  if (document.getElementById('ow-gpt-welcome')) return;
  const overlay = document.createElement('div');
  overlay.id = 'ow-gpt-welcome';
  overlay.className = 'modal';
  overlay.innerHTML = `<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="ow-gpt-welcome-title">
      <div class="modal-body">
        <div class="ow-gpt-welcome-logo"><span>${gptLogo(26, '#fff')}</span></div>
        <h4 id="ow-gpt-welcome-title" style="text-align:center;margin:0 0 8px;">You're using your ChatGPT plan</h4>
        <p style="margin:0 0 8px;">AI requests you send to ChatGPT models in OpenWings now use your ChatGPT plan. You can review usage and set limits for OpenWings in ChatGPT settings.</p>
        <p style="margin:0;font-size:12px;opacity:0.75;">On ChatGPT Plus, the five-hour limit is shared with every app that uses your plan.</p>
      </div>
      <div class="modal-footer">
        <a class="confirm-btn confirm-btn-secondary" href="${CHATGPT_USAGE_URL}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">Manage usage</a>
        <button type="button" class="confirm-btn confirm-btn-primary" data-ow-gpt-gotit>Got it</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.classList.remove('hidden');
  overlay.style.display = '';
  const done = () => {
    overlay.remove();
    fetch('/api/wings/connect/chatgpt/welcomed', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
  };
  overlay.querySelector('[data-ow-gpt-gotit]').addEventListener('click', done);
  overlay.querySelector('[data-ow-gpt-gotit]').focus();
}

// "Using ChatGPT plan" beside the model picker while a ChatGPT plan model
// is selected (the picker label's title carries the route address).
const PLAN_ROUTE = 'https://api.openai.com/v1/responses';
function updatePlanChip() {
  const label = document.getElementById('model-picker-label');
  const wrap = document.getElementById('model-picker-wrap');
  if (!label || !wrap) return;
  let chip = document.getElementById('ow-gpt-plan-chip');
  if (!chip) {
    chip = document.createElement('div');
    chip.id = 'ow-gpt-plan-chip';
    chip.className = 'ow-gpt-chip';
    chip.hidden = true;
    chip.innerHTML = `<span class="ow-gpt-mark">${gptLogo(10, '#fff')}</span><span>Using ChatGPT plan</span><a href="${CHATGPT_USAGE_URL}" target="_blank" rel="noopener noreferrer">Manage usage</a>`;
    wrap.appendChild(chip);
  }
  const using = String(label.title || '').split(' · ').includes(PLAN_ROUTE);
  chip.hidden = !using;
}
function watchPlanChip() {
  const label = document.getElementById('model-picker-label');
  if (!label) return;
  updatePlanChip();
  new MutationObserver(updatePlanChip).observe(label, { attributes: true, attributeFilter: ['title'] });
}

function waitFor(fn, timeoutMs = 4000, stepMs = 100) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const tick = () => {
      const v = fn();
      if (v) return resolve(v);
      if (Date.now() - t0 > timeoutMs) return resolve(null);
      setTimeout(tick, stepMs);
    };
    tick();
  });
}

async function openClaudeAgentSetup() {
  settingsModule.open('integrations');
  const add = await waitFor(() => document.getElementById('unified-intg-add-btn'));
  if (!add) return;
  for (let i = 0; i < 20; i++) {
    if (!document.querySelector('.uf-add-menu')) add.click();
    const opt = await waitFor(() => document.querySelector('.uf-add-menu .uf-type-option[data-value="claude"]'), 300, 50);
    if (opt) { opt.click(); return; }
  }
}

function wireAiPanel(root, onAdded) {
  syncKeyLink(root);
  renderChatgpt(root);
  root.addEventListener('change', (e) => {
    if (e.target.matches('[data-ow-key-provider]')) syncKeyLink(root);
  });
  root.addEventListener('input', (e) => {
    if (!e.target.matches('[data-ow-key-input]')) return;
    const key = e.target.value.trim();
    const hit = KEY_PREFIXES.find(([re]) => re.test(key));
    const sel = root.querySelector('[data-ow-key-provider]');
    if (hit && sel && sel.value !== hit[1] && Array.from(sel.options).some((o) => o.value === hit[1])) {
      sel.value = hit[1];
      syncKeyLink(root);
    }
  });
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-ow-act]');
    if (!btn || !root.contains(btn)) return;
    const act = btn.dataset.owAct;
    if (act === 'local-scan') scanLocal(root, onAdded);
    else if (act === 'openrouter') connectOpenRouter(root);
    else if (act === 'openrouter-finish') finishOpenRouter(root, onAdded);
    else if (act === 'key-test') testKey(root);
    else if (act === 'key-add') addKey(root, onAdded);
    else if (act === 'claude-agent') openClaudeAgentSetup();
    else if (act === 'gpt-start') startChatgpt(root, { consent: btn.dataset.owConsent === '1' }, onAdded);
    else if (act === 'gpt-signout') signOutChatgpt(root);
    else if (act === 'gpt-forget') forgetChatgpt(root);
    else if (act === 'gpt-models') reloadChatgptModels(root, onAdded);
    else if (act === 'gpt-focus') {
      const c = root.querySelector('[data-ow-choice="chatgpt"]');
      if (c) { c.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); c.classList.add('ow-flash'); setTimeout(() => c.classList.remove('ow-flash'), 1200); }
    }
    else if (act === 'toggle') {
      const panel = root.querySelector(`[data-ow-panel="${btn.dataset.owTarget}"]`);
      const open = !!(panel && panel.hidden);
      if (panel) panel.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (btn.dataset.owTarget === 'key') btn.textContent = open ? 'Hide' : 'Paste a key';
      if (open && panel) {
        const first = panel.querySelector('input, select');
        if (first && btn.dataset.owTarget === 'key') first.focus();
        setTimeout(() => panel.scrollIntoView({ block: 'nearest' }), 0);
      }
      refit();
    }
  });
}

// ------------------------------------------------------------ first run

// The welcome card is placed by the current look (centred, or anchored above
// the message box), which leaves the first-run choices little room. While
// they show, the message box moves to the bottom (.ow-first-run, see the
// style above) and the card is placed in the free band between the top bar
// and the message box, centred when it fits; only what does not fit scrolls
// inside the panel, with a fade and a "More below" cue at its bottom edge.
const WS_PROPS = ['top', 'bottom', 'transform', 'translate'];
function clearWelcomePlacement(ws, scroller) {
  WS_PROPS.forEach((p) => ws.style.removeProperty(p));
  if (scroller) scroller.style.removeProperty('max-height');
}
function updateMoreCue() {
  const scroller = document.querySelector('#welcome-setup .ow-setup-scroll');
  if (!scroller) return;
  scroller.classList.toggle('ow-more', scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 4);
}
function fitWelcome() {
  const ws = document.getElementById('welcome-screen');
  const scroller = document.querySelector('#welcome-setup .ow-setup-scroll');
  if (!ws) return;
  const active = ws.classList.contains('ow-has-setup') && !ws.classList.contains('hidden') && !!scroller;
  const container = ws.closest('.chat-container');
  if (container) container.classList.toggle('ow-first-run', active);
  if (!active) { clearWelcomePlacement(ws, scroller); return; }
  clearWelcomePlacement(ws, scroller);
  const bar = document.querySelector('.chat-input-bar');
  const topBar = document.querySelector('.chat-top-bar');
  const limitTop = (topBar ? topBar.getBoundingClientRect().bottom : 0) + 10;
  const barTop = bar && bar.getClientRects().length ? bar.getBoundingClientRect().top : window.innerHeight;
  const limitBottom = (barTop > limitTop + 160 ? barTop : window.innerHeight) - 10;
  const avail = Math.max(200, limitBottom - limitTop);
  const cueH = 18;
  const wsH = ws.getBoundingClientRect().height;
  const listH = scroller.getBoundingClientRect().height;
  const chrome = Math.max(0, wsH - listH);
  const fits = wsH <= avail;
  const listMax = fits ? listH : Math.max(140, Math.floor(avail - chrome - cueH));
  if (!fits) scroller.style.maxHeight = `${listMax}px`;
  const finalH = fits ? wsH : chrome + listMax + cueH;
  const parent = ws.offsetParent || document.body;
  const pr = parent.getBoundingClientRect();
  const top = limitTop + Math.max(0, (avail - finalH) / 2) - pr.top;
  ws.style.setProperty('top', `${Math.round(top)}px`, 'important');
  ws.style.setProperty('bottom', 'auto', 'important');
  ws.style.setProperty('transform', 'translateX(-50%)', 'important');
  ws.style.setProperty('translate', 'none', 'important');
  updateMoreCue();
}
let _fitQueued = false;
function refit() {
  if (_fitQueued) return;
  _fitQueued = true;
  requestAnimationFrame(() => { _fitQueued = false; fitWelcome(); });
}

function reloadSoon(msgEl) {
  showMsg(msgEl);
  setTimeout(() => window.location.reload(), 1600);
}

async function renderWelcome() {
  const box = document.getElementById('welcome-setup');
  const screen = document.getElementById('welcome-screen');
  if (!box || !screen) return;
  const st = await fetchStatus();
  if (!st.admin) {
    screen.classList.remove('ow-has-setup');
    // A member cannot add an AI; say whom to ask while there is none.
    if (st.ai_ready === false) {
      box.innerHTML = '<div class="ow-member-note">No AI is connected yet. Ask your OpenWings admin to connect one.</div>';
      box.style.display = '';
    } else {
      box.style.display = 'none';
    }
    return;
  }
  // Shown until some endpoint actually has models (an endpoint whose key the
  // provider refused has none, and leaves nothing to chat with).
  const usable = typeof st.usable_endpoints === 'number' ? st.usable_endpoints : st.model_endpoints;
  if (usable !== 0) {
    box.style.display = 'none';
    screen.classList.remove('ow-has-setup');
    fitWelcome();
    return;
  }
  // The title stays put; only the choices scroll, with a fade and a cue
  // at the bottom edge while more is hidden below.
  box.innerHTML = `<div class="ow-welcome-head">Connect your AI</div><div class="ow-setup-scroll">${aiPanelHtml('welcome')}</div><div class="ow-more-cue" aria-hidden="true">More below ↓</div>`;
  box.style.display = '';
  screen.classList.add('ow-has-setup');
  const root = box.querySelector('.ow-connect');
  wireAiPanel(root, (msgEl) => reloadSoon(msgEl));
  box.querySelector('.ow-setup-scroll').addEventListener('scroll', updateMoreCue, { passive: true });
  window.addEventListener('resize', refit);
  new MutationObserver(refit).observe(screen, { attributes: true, attributeFilter: ['class'] });
  new MutationObserver(refit).observe(box, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] });
  refit();
  setTimeout(refit, 600);
  scanLocal(root, (msgEl) => reloadSoon(msgEl));
}

// ------------------------------------------------------------ Settings cards

async function renderSettingsAiCard() {
  const panel = document.querySelector('[data-settings-panel="services"]');
  if (!panel || document.getElementById('ow-connect-ai-card')) return;
  const st = await fetchStatus();
  if (!st.admin) return;
  const card = document.createElement('div');
  card.className = 'admin-card';
  card.id = 'ow-connect-ai-card';
  card.innerHTML = `<h2>${AI_ICON}Connect your AI</h2>${aiPanelHtml('settings')}`;
  panel.insertBefore(card, panel.firstChild);
  const root = card.querySelector('.ow-connect');
  wireAiPanel(root, () => {
    try { settingsModule.refreshAiModelEndpoints && settingsModule.refreshAiModelEndpoints(); } catch (_) {}
  });
}

// What the card says. `signed_in` (from GET /api/mcp/servers) tells a
// first connection apart from one that worked before.
function appState(app, servers) {
  const norm = (u) => String(u || '').replace(/\/+$/, '');
  const srv = servers.find((s) => norm(s.url) === norm(app.url));
  if (!srv) return { srv: null, label: 'Not connected', dot: '', fresh: true };
  const before = srv.signed_in === true;
  if (srv.status === 'connected') return { srv, label: `Connected · ${srv.enabled_tool_count ?? srv.tool_count ?? 0} tools`, dot: 'on' };
  if (srv.status === 'needs_auth') return { srv, label: 'Waiting for sign-in', dot: 'warn' };
  if (srv.status === 'connecting') return { srv, label: 'Connecting…', dot: 'warn' };
  if (srv.status === 'error') {
    return before
      ? { srv, label: 'Needs a new sign-in', dot: 'warn', error: srv.error }
      : { srv, label: 'Could not connect', dot: 'warn', error: srv.error, fresh: true };
  }
  if (!before) return { srv, label: 'Not connected', dot: '', fresh: true };
  return { srv, label: srv.is_enabled === false ? 'Turned off' : 'Not connected right now', dot: 'warn' };
}

async function loadServers() {
  try {
    const r = await fetch('/api/mcp/servers', { credentials: 'same-origin' });
    const d = r.ok ? await r.json() : [];
    return Array.isArray(d) ? d : (d.servers || []);
  } catch (_) { return []; }
}

const AI_ICON = '<svg class="ow-card-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>';
const APP_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>';

function safeAuthUrl(u) {
  try { return new URL(u).protocol === 'https:' ? u : null; } catch (_) { return null; }
}

async function connectApp(card, slug) {
  const appCard = card.querySelector(`[data-ow-app="${CSS.escape(slug)}"]`);
  const msg = appCard && appCard.querySelector('[data-ow-app-msg]');
  // Open the tab now, while the click still counts, then point it at the
  // sign-in page once the server has it (discovery and registration first).
  let tab = null;
  try {
    tab = window.open('', '_blank');
    if (tab) {
      try { tab.opener = null; } catch (_) {}
      try { tab.document.title = 'Opening sign-in…'; tab.document.body.textContent = 'Opening the sign-in page…'; } catch (_) {}
    }
  } catch (_) { tab = null; }
  setMsg(msg, 'Opening the sign-in page…');
  appCard?.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  // Say at once what is happening, not "Not connected" with a Connect button.
  const label = appCard && appCard.querySelector('[data-ow-app-label]');
  if (label) label.textContent = 'Waiting for sign-in';
  const dot = appCard && appCard.querySelector('.ow-app-dot');
  if (dot) dot.className = 'ow-app-dot warn';
  appCard?.querySelector('.ow-app-error')?.remove();
  const pressed = appCard && appCard.querySelector(`[data-ow-app-connect="${CSS.escape(slug)}"]`);
  if (pressed) pressed.textContent = 'Waiting…';
  let serverId = null;
  try {
    const r = await fetch(`/api/wings/connect/apps/${encodeURIComponent(slug)}`, { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    serverId = d.id;
    if (d.status === 'error') throw new Error(d.error || 'the app did not answer');
    if (d.connected || d.status === 'connected') {
      if (tab) tab.close();
      setMsg(msg, 'Connected.', 'ok');
      return renderAccounts(card);
    }
    let authUrl = safeAuthUrl(d.auth_url);
    for (let i = 0; !authUrl && i < 20; i++) {
      await new Promise((res) => setTimeout(res, 1500));
      const s = (await loadServers()).find((x) => x.id === serverId);
      if (s && s.status === 'error') throw new Error(s.error || 'the app did not answer');
      if (s && s.status === 'connected') { if (tab) tab.close(); return renderAccounts(card); }
      authUrl = safeAuthUrl(s && s.auth_url);
    }
    if (!authUrl) throw new Error('the sign-in page did not come up');
    if (tab && !tab.closed) tab.location.href = authUrl;
    msg.innerHTML = `Sign in on the page that opened (<a href="${esc(authUrl)}" target="_blank" rel="noopener noreferrer">open it again</a>). This card updates when you are done.`;
    msg.className = 'adm-ep-inline-msg';
    // When the sign-in returns to a different address than this browser is
    // using (an address this install does not list), offer paste-back.
    let backHost = '';
    try { backHost = new URL(new URL(authUrl).searchParams.get('redirect_uri') || '').host; } catch (_) {}
    if (backHost && backHost !== window.location.host) {
      const wrap = document.createElement('div');
      wrap.innerHTML = '<div class="ow-app-note" style="opacity:0.8">Sign-in did not come back here? Paste the address it ended on:</div>'
        + '<div class="ow-row"><input type="text" class="settings-input" placeholder="Paste the address here" aria-label="Address the sign-in ended on"><button type="button" class="admin-btn-sm">Send</button></div>'
        + '<div class="adm-ep-inline-msg" data-ow-paste-msg></div>';
      msg.appendChild(wrap);
      const pasteMsg = wrap.querySelector('[data-ow-paste-msg]');
      wrap.querySelector('button').addEventListener('click', async () => {
        const v = wrap.querySelector('input').value.trim();
        if (!v) return setMsg(pasteMsg, 'Paste the address first.', 'err');
        const f = new FormData();
        f.append('callback_url', v);
        setMsg(pasteMsg, 'Sending…');
        try {
          const r = await fetch(`/api/mcp/oauth/exchange/${encodeURIComponent(serverId)}`, { method: 'POST', body: f, credentials: 'same-origin' });
          if (r.ok) setMsg(pasteMsg, 'Got it, finishing the connection…', 'ok');
          else setMsg(pasteMsg, 'That address did not work. Copy the whole address from the sign-in tab and send it again.', 'err');
        } catch (_) {
          setMsg(pasteMsg, 'That address did not work. Copy the whole address from the sign-in tab and send it again.', 'err');
        }
      });
    }
    for (let i = 0; i < 150; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      const s = (await loadServers()).find((x) => x.id === serverId);
      if (!s) break;
      if (s.status === 'connected') { await renderAccounts(card); return; }
      if (s.status === 'error') throw new Error(s.error || 'sign-in failed');
      if (s.status !== 'needs_auth' && s.status !== 'connecting') {
        // Cancelled on the app's own page: the server ended the sign-in.
        await renderAccounts(card);
        const m = card.querySelector(`[data-ow-app="${CSS.escape(slug)}"] [data-ow-app-msg]`);
        setMsg(m, 'Sign-in cancelled. Nothing was connected.');
        return;
      }
    }
    await renderAccounts(card);
  } catch (e) {
    if (tab && !tab.closed) { try { tab.close(); } catch (_) {} }
    await renderAccounts(card);
    const m = card.querySelector(`[data-ow-app="${CSS.escape(slug)}"] [data-ow-app-msg]`);
    // The card itself now shows the reason under its label.
    if (!m || !m.closest('[data-ow-app]')?.querySelector('.ow-app-error')) {
      setMsg(m, 'Could not connect: ' + (e.message || 'request failed'), 'err');
    }
  }
}

async function disconnectApp(card, serverId, name) {
  const ok = window.styledConfirm
    ? await window.styledConfirm(`Disconnect ${name}? OpenWings forgets its sign-in; you can connect again any time.`, { confirmText: 'Disconnect', danger: true })
    : window.confirm(`Disconnect ${name}?`);
  if (!ok) return;
  try { await fetch(`/api/mcp/servers/${encodeURIComponent(serverId)}`, { method: 'DELETE', credentials: 'same-origin' }); } catch (_) {}
  await renderAccounts(card);
}

// ------------------------------------------------------------ Connect your accounts (patch 0025)
//
// One card in Settings > Integrations for every account a person can
// connect: mail and calendar, files and notes, work apps. Each person
// connects their own (the work apps are connected by an admin for everyone,
// as in patch 0021). Back end: wings/accounts_routes.py.
//
// Cards are an ordered list so a later patch can add one in its place:
// patch 0026 puts "Outlook / Microsoft 365" right after Yahoo (see
// ACCOUNT_CARDS and addAccountCard).

const ACCOUNT_GROUPS = [
  { id: 'mail', title: 'Mail and calendar' },
  { id: 'files', title: 'Files and notes' },
  { id: 'work', title: 'Work apps' },
];

const MAIL_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><polyline points="3 7 12 13 21 7"/></svg>';
const CAL_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>';
const LINK_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';

// Card list. kind: 'mail' (app password), 'google', 'feed', 'app' (0021 apps).
const ACCOUNT_CARDS = [
  { id: 'gmail', group: 'mail', kind: 'mail', provider: 'gmail', name: 'Gmail', icon: MAIL_ICON, about: 'Your Gmail inbox and sending, with an app password.' },
  { id: 'google', group: 'mail', kind: 'google', name: 'Google Calendar and Drive', icon: CAL_ICON, about: 'Sign in with Google: Gmail, your main Google calendar with Meet links, and Drive. Needs a free one-time setup by your admin.', aboutReady: 'Sign in with Google: Gmail, your main Google calendar with Meet links, and Drive.' },
  { id: 'icloud', group: 'mail', kind: 'mail', provider: 'icloud', name: 'iCloud', icon: MAIL_ICON, about: 'iCloud Mail and, if you like, iCloud Calendar.' },
  { id: 'yahoo', group: 'mail', kind: 'mail', provider: 'yahoo', name: 'Yahoo', icon: MAIL_ICON, about: 'Your Yahoo inbox and sending, with an app password.' },
  // Patch 0026 adds { id: 'microsoft', ... } "Outlook / Microsoft 365" here.
  { id: 'calendar-link', group: 'mail', kind: 'feed', name: 'Calendar by link', icon: LINK_ICON, about: 'Any calendar with a share link: Google, Outlook, iCloud and more. Read only.' },
  { id: 'notion', group: 'files', kind: 'app', slug: 'notion' },
  { id: 'linear', group: 'work', kind: 'app', slug: 'linear' },
  { id: 'atlassian', group: 'work', kind: 'app', slug: 'atlassian' },
];

// For a later patch: add a card after another one (or at the end).
function addAccountCard(card, { after } = {}) {
  if (ACCOUNT_CARDS.some((c) => c.id === card.id)) return;
  const at = after ? ACCOUNT_CARDS.findIndex((c) => c.id === after) : -1;
  if (at < 0) ACCOUNT_CARDS.push(card);
  else ACCOUNT_CARDS.splice(at + 1, 0, card);
  const host = document.getElementById('ow-connect-apps-card');
  if (host) renderAccounts(host);
}
window.owAccounts = { addAccountCard, open: (id) => openAccountCard(id) };

const ACCOUNT_ERRORS = {
  denied: 'Google sign-in was cancelled. Nothing was changed.',
  expired: 'That sign-in ran out of time or was already used. Press Sign in with Google again.',
  other_user: 'That sign-in was started by another OpenWings account. Press Sign in with Google again.',
  not_signed_in: 'Sign in to OpenWings in this browser first, then try again.',
  no_client: 'Sign in with Google is not set up on this OpenWings yet. Ask your admin.',
  invalid_client: 'Google did not accept this OpenWings\' client ID or secret. Ask your admin to check them.',
  redirect_mismatch: 'Google says the redirect address does not match. Ask your admin to check the redirect URI in the Google console.',
  exchange_failed: 'Google did not hand over access. Please try again.',
  no_email: 'Google did not say which account signed in. Please try again.',
  save_failed: 'You signed in, but the account could not be saved. Please try again.',
  missing_code: 'The sign-in came back without a code. Please try again.',
  no_code: 'The sign-in came back without a code. Please try again.',
};
const FEATURE_NAMES = { mail: 'Mail', calendar: 'Calendar', drive: 'Drive' };
const listWords = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);

let _accounts = null;

async function fetchAccounts() {
  try {
    const r = await fetch('/api/wings/accounts/status', { credentials: 'same-origin' });
    _accounts = r.ok ? await r.json() : null;
  } catch (_) { _accounts = null; }
  return _accounts;
}

async function postJson(url, body, method = 'POST') {
  const r = await fetch(url, {
    method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok && d.ok !== false, status: r.status, data: d };
}

function cardShell(c, { label, dot = '', buttons = [], error = '', note = '' }) {
  return `<div class="intg-card" data-ow-acct="${esc(c.id)}">
    <div class="ow-app-head"><span style="color:var(--accent, var(--red));display:inline-flex;">${c.icon || APP_ICON}</span>${esc(c.name)}</div>
    <div class="ow-app-about">${esc(c.about || '')}</div>
    <div class="ow-app-state"><span class="ow-app-dot ${dot}"></span><span data-ow-app-label>${esc(label)}</span></div>
    ${error ? `<div class="ow-app-error">${esc(error)}</div>` : ''}
    <div class="ow-app-actions">${buttons.join('')}</div>
    <div class="adm-ep-inline-msg" data-ow-app-msg></div>
    ${note ? `<div class="ow-app-note">${esc(note)}</div>` : ''}
  </div>`;
}

const btn = (label, attrs, primary = false) => `<button type="button" class="${primary ? 'admin-btn-add' : 'admin-btn-sm'}" ${attrs}>${esc(label)}</button>`;

function mailCardHtml(c, acc) {
  const mine = (acc.mail || []).filter((m) => m.provider === c.provider);
  if (!mine.length) {
    const gmailOAuth = c.provider === 'gmail' && (acc.mail || []).some((m) => m.provider === 'google_oauth');
    return cardShell(c, {
      label: gmailOAuth ? 'Connected with Sign in with Google' : 'Not connected',
      dot: gmailOAuth ? 'on' : '',
      buttons: gmailOAuth ? [] : [btn('Connect', `data-ow-acct-open="${esc(c.id)}"`, true)],
    });
  }
  const buttons = [btn('Add another', `data-ow-acct-open="${esc(c.id)}"`)];
  mine.forEach((m) => buttons.push(btn(mine.length > 1 ? `Disconnect ${m.address}` : 'Disconnect',
    `data-ow-mail-remove="${esc(m.id)}" data-ow-name="${esc(m.address)}"`)));
  const cal = c.provider === 'icloud' && (acc.caldav || []).some((x) => x.kind === 'icloud');
  return cardShell(c, {
    label: `Connected as ${mine.map((m) => m.address).join(', ')}${cal ? ' · Calendar too' : ''}`,
    dot: 'on', buttons,
  });
}

function googleCardHtml(c, acc) {
  const g = acc.google || {};
  const links = g.links || [];
  if (g.configured && c.aboutReady) c = { ...c, about: c.aboutReady };
  if (!g.configured) {
    const buttons = acc.admin ? [btn('Set up (admin, once)', 'data-ow-google-setup', true)] : [];
    return cardShell(c, {
      label: acc.admin ? 'Needs a one-time setup' : 'Ask your OpenWings admin to set this up',
      buttons,
      note: 'Google charges for the review that a shared one-click Gmail needs, so each OpenWings uses its own free Google key. The admin makes it once, for everyone here.',
    });
  }
  if (!links.length) {
    const buttons = [btn('Sign in with Google', `data-ow-acct-open="google"`, true)];
    if (acc.admin) buttons.push(btn('Google key', 'data-ow-google-setup'));
    return cardShell(c, { label: 'Not connected', buttons });
  }
  const buttons = [btn('Sign in again or add more', `data-ow-acct-open="google"`)];
  links.forEach((l) => buttons.push(btn(links.length > 1 ? `Disconnect ${l.account}` : 'Disconnect',
    `data-ow-google-remove="${esc(l.id)}" data-ow-name="${esc(l.account)}"`)));
  if (acc.admin) buttons.push(btn('Google key', 'data-ow-google-setup'));
  const what = (l) => ['mail', 'calendar', 'drive'].filter((k) => l[k]).map((k) => ({ mail: 'Mail', calendar: 'Calendar', drive: 'Drive' }[k])).join(', ') || 'nothing yet';
  const needs = links.some((l) => l.needs_signin);
  return cardShell(c, {
    label: needs ? 'Needs a new sign-in' : `Connected as ${links.map((l) => `${l.account} (${what(l)})`).join('; ')}`,
    dot: needs ? 'warn' : 'on', buttons,
  });
}

function feedCardHtml(c, acc) {
  const feeds = acc.feeds || [];
  const buttons = [btn(feeds.length ? 'Add another' : 'Add a calendar', `data-ow-acct-open="calendar-link"`, !feeds.length)];
  if (!feeds.length) return cardShell(c, { label: 'No calendars added', buttons });
  const rows = feeds.map((f) => `<div class="ow-feed-row" data-ow-feed="${esc(f.id)}">
      <span class="ow-feed-dot" style="background:${/^#[0-9a-f]{6}$/i.test(f.color || '') ? f.color : 'var(--accent, var(--red))'}"></span>
      <span class="ow-feed-name">${esc(f.name)}<span class="ow-feed-sub">${f.error ? esc(f.error) : `${f.events} events · read only`}</span></span>
      <button type="button" class="admin-btn-sm" data-ow-feed-refresh="${esc(f.id)}">Refresh now</button>
      <button type="button" class="admin-btn-sm" data-ow-feed-remove="${esc(f.id)}" data-ow-name="${esc(f.name)}">Remove</button>
    </div>`).join('');
  return cardShell(c, { label: `${feeds.length} calendar${feeds.length > 1 ? 's' : ''} added`, dot: feeds.some((f) => f.error) ? 'warn' : 'on', buttons })
    .replace('<div class="ow-app-actions">', `<div class="ow-feed-list">${rows}</div><div class="ow-app-actions">`);
}

function appCardHtml(c, app, servers, admin) {
  const card = { ...c, name: app.name, about: app.about, icon: APP_ICON };
  if (!admin) {
    // Only what is true right now: connected by the admin, or not set up yet.
    return app.connected
      ? cardShell(card, { label: 'Connected by your admin for everyone', dot: 'on', note: 'Your OpenWings admin connected this once for everyone here.' })
      : cardShell(card, { label: 'Not set up yet. Ask your OpenWings admin.', note: 'Your OpenWings admin connects this once for everyone here.' });
  }
  const s = appState(app, servers);
  const connected = s.dot === 'on';
  const buttons = [];
  const finishUrl = s.srv && s.srv.status === 'needs_auth' ? safeAuthUrl(s.srv.auth_url) : null;
  if (!s.srv || (s.fresh && !finishUrl)) {
    buttons.push(`<button type="button" class="admin-btn-add" data-ow-app-connect="${esc(app.slug)}">${s.error ? 'Try again' : 'Connect'}</button>`);
  } else {
    if (finishUrl) {
      buttons.push(`<a class="admin-btn-add" style="text-decoration:none;" href="${esc(finishUrl)}" target="_blank" rel="noopener noreferrer">Finish sign-in</a>`);
      buttons.push(`<button type="button" class="admin-btn-sm" data-ow-app-connect="${esc(app.slug)}">Start over</button>`);
    } else if (!connected) buttons.push(`<button type="button" class="admin-btn-add" data-ow-app-connect="${esc(app.slug)}">Reconnect</button>`);
    buttons.push(`<button type="button" class="admin-btn-sm" data-ow-app-disconnect="${esc(s.srv.id)}" data-ow-app-name="${esc(app.name)}">Disconnect</button>`);
  }
  return cardShell(card, { label: s.label, dot: s.dot, buttons, error: s.error, note: app.note || 'Everyone on this OpenWings can use it with your account.' })
    .replace(`data-ow-acct="${esc(c.id)}"`, `data-ow-acct="${esc(c.id)}" data-ow-app="${esc(app.slug)}"`);
}

async function renderAccounts(card) {
  const [acc, st] = await Promise.all([fetchAccounts(), fetchStatus()]);
  const body = card.querySelector('.ow-acct-groups');
  if (!body) return;
  if (!acc) { body.innerHTML = '<div class="ow-app-note">Could not load your accounts. Reload the page to try again.</div>'; return; }
  const apps = acc.apps || [];
  const servers = acc.admin ? await loadServers() : [];
  body.innerHTML = ACCOUNT_GROUPS.map((g) => {
    const cards = ACCOUNT_CARDS.filter((c) => c.group === g.id).map((c) => {
      if (c.render) return c.render(c, acc);
      if (c.kind === 'mail') return mailCardHtml(c, acc);
      if (c.kind === 'google') return googleCardHtml(c, acc);
      if (c.kind === 'feed') return feedCardHtml(c, acc);
      if (c.kind === 'app') {
        const app = apps.find((a) => a.slug === c.slug);
        return app ? appCardHtml(c, app, servers, acc.admin) : '';
      }
      return '';
    }).join('');
    return cards ? `<div class="ow-acct-group"><div class="ow-acct-group-title">${esc(g.title)}</div><div class="ow-apps-grid">${cards}</div></div>` : '';
  }).join('') + '<div class="ow-acct-notpossible">Not possible: Google Keep and Apple Notes, because Google and Apple offer no way for other apps to open them. Zoom needs its own app setup, so it comes later.</div>';
  void st;
}

// ---- dialogs

// Dialogs: upstream's window look (modal, modal-content), on top of Settings.
// Escape closes the newest one through upstream's Escape arbiter (never the
// window behind it), Tab stays inside, and focus goes back to the button
// that opened it.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
const _openDialogs = [];

function openDialog(id, title, html, { wide = false } = {}) {
  const old = document.getElementById(id);
  if (old) { if (typeof old.close === 'function') old.close({ keepFocus: true }); else old.remove(); }
  const opener = document.activeElement;
  const openerCard = (opener && opener.closest && opener.closest('[data-ow-acct]')?.dataset.owAcct) || opener?.dataset?.owAcctOpen || '';
  const overlay = document.createElement('div');
  overlay.id = id;
  overlay.className = 'modal ow-acct-dialog';
  overlay.style.zIndex = '300';
  overlay.innerHTML = `<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="${id}-title" style="width:${wide ? 560 : 460}px;max-width:calc(100vw - 24px);">
      <div class="modal-header"><h4 id="${id}-title">${esc(title)}</h4><button type="button" class="close-btn" data-ow-close aria-label="Close">✖</button></div>
      <div class="modal-body ow-acct-body">${html}</div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.classList.remove('hidden');
  overlay.style.display = 'flex';
  const box = overlay.querySelector('.modal-content');
  let closed = false;
  let unregister = () => {};
  const close = ({ keepFocus = false } = {}) => {
    if (closed) return;
    closed = true;
    unregister();
    document.removeEventListener('keydown', onKey);
    const i = _openDialogs.indexOf(overlay);
    if (i !== -1) _openDialogs.splice(i, 1);
    overlay.remove();
    if (!keepFocus) {
      const back = (opener && opener.isConnected && opener !== document.body) ? opener
        : (openerCard && document.querySelector(`#ow-connect-apps-card [data-ow-acct="${CSS.escape(openerCard)}"] button`));
      try { back?.focus(); } catch (_) {}
    }
  };
  unregister = registerEscapeLayer(() => close(), () => !closed && overlay.isConnected);
  const onKey = (e) => {
    if (_openDialogs[_openDialogs.length - 1] !== overlay) return;
    const confirmOpen = document.getElementById('styled-confirm-overlay');
    if (confirmOpen && !confirmOpen.classList.contains('hidden') && confirmOpen.style.display !== 'none') return;
    if (e.key === 'Escape' && !e.defaultPrevented) {
      // Fallback only: upstream's arbiter normally handles Escape first.
      e.preventDefault(); e.stopPropagation(); close();
      return;
    }
    if (e.key !== 'Tab') return;
    const items = [...box.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !box.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  };
  _openDialogs.push(overlay);
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay || e.target.closest('[data-ow-close]')) close();
  });
  overlay.close = close;
  setTimeout(() => (overlay.querySelector('input:not([type=checkbox]), button.admin-btn-add') || box)?.focus(), 30);
  return overlay;
}

// upstream's styledConfirm, as the newest Escape layer: Escape closes the
// question, not the dialog or window under it.
async function askConfirm(text, opts) {
  if (!window.styledConfirm) return window.confirm(text);
  const unregister = registerEscapeLayer(() => {
    document.getElementById('styled-confirm-cancel')?.click();
  }, () => {
    const o = document.getElementById('styled-confirm-overlay');
    return !!o && !o.classList.contains('hidden');
  });
  try { return await window.styledConfirm(text, opts); } finally { unregister(); }
}

function stepsHtml(steps) {
  return `<ol class="ow-acct-steps">${steps.map((s) => `<li>${esc(s.text)}${s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.link || 'Open')}</a>` : ''}</li>`).join('')}</ol>`;
}

async function openMailDialog(providerKey) {
  const acc = _accounts || await fetchAccounts();
  const p = ((acc && acc.mail_providers) || []).find((x) => x.key === providerKey);
  if (!p) return;
  const google = providerKey === 'gmail' && acc.google && acc.google.configured;
  const html = `
    ${google ? `<div class="ow-acct-choice"><div class="ow-acct-lead">Easiest: Sign in with Google. Google's own page opens; no password to copy.</div>
      <button type="button" class="admin-btn-add" data-ow-goto-google>Sign in with Google</button></div>
      <div class="ow-acct-or">Or use an app password</div>` : ''}
    <div class="ow-acct-lead">${esc(p.intro)}</div>
    ${stepsHtml(p.steps)}
    ${p.unavailable ? `<details class="ow-acct-details" data-ow-unavailable><summary>No App passwords option?</summary><div>${esc(p.unavailable)}</div></details>` : ''}
    <label class="ow-acct-label" for="ow-mail-address">Email address</label>
    <input id="ow-mail-address" class="settings-input" type="email" autocomplete="email" placeholder="${esc(p.example)}">
    <label class="ow-acct-label" for="ow-mail-password">App password</label>
    <input id="ow-mail-password" class="settings-input" type="password" autocomplete="off" spellcheck="false" placeholder="Paste the app password">
    ${p.calendar ? `<label class="ow-acct-check"><input type="checkbox" id="ow-mail-calendar" checked> Also sync my ${esc(p.name)} calendars</label>` : ''}
    <div class="ow-acct-actions"><button type="button" class="admin-btn-add" data-ow-mail-connect>Connect</button><button type="button" class="admin-btn-sm" data-ow-close>Cancel</button></div>
    <div class="adm-ep-inline-msg" data-ow-dialog-msg></div>
    <div class="ow-app-note">OpenWings tests the password with ${esc(p.name)} first and saves it encrypted. It is used only for your mailbox. <a href="${esc(p.help)}" target="_blank" rel="noopener noreferrer">${esc(p.name)} help</a></div>`;
  const dlg = openDialog('ow-mail-dialog', `Connect ${p.name}`, html);
  const msg = dlg.querySelector('[data-ow-dialog-msg]');
  dlg.querySelector('[data-ow-goto-google]')?.addEventListener('click', () => { dlg.close(); openGoogleDialog(); });
  dlg.querySelector('#ow-mail-password').addEventListener('input', (e) => {
    const v = e.target.value; const clean = v.replace(/\s+/g, '');
    if (v !== clean) e.target.value = clean;
  });
  const go = async () => {
    const address = dlg.querySelector('#ow-mail-address').value.trim();
    const password = dlg.querySelector('#ow-mail-password').value.replace(/\s+/g, '');
    if (!address) return setMsg(msg, 'Type your email address.', 'err');
    if (!password) return setMsg(msg, 'Paste the app password.', 'err');
    const b = dlg.querySelector('[data-ow-mail-connect]');
    b.disabled = true;
    setMsg(msg, `Checking with ${p.name}…`);
    const r = await postJson('/api/wings/accounts/mail/connect', {
      provider: providerKey, address, password, calendar: !!dlg.querySelector('#ow-mail-calendar')?.checked,
    });
    b.disabled = false;
    if (!r.ok) {
      // A refused password: the account may simply have no app passwords.
      if (r.data.error === 'wrong_password') { const d = dlg.querySelector('[data-ow-unavailable]'); if (d) d.open = true; }
      return setMsg(msg, r.data.message || r.data.detail || 'That did not work. Please try again.', 'err');
    }
    let done = `Connected ${r.data.address}. Your mail shows up in Email in a minute.`;
    if (r.data.calendar === 'syncing') done += ' Your calendars are syncing.';
    if (r.data.calendar === 'failed') done += ` Calendars were not added: ${r.data.calendar_error || 'please try again'}.`;
    setMsg(msg, done, 'ok');
    dlg.querySelector('#ow-mail-password').value = '';
    const host = document.getElementById('ow-connect-apps-card');
    if (host) renderAccounts(host);
    setTimeout(() => dlg.close(), 1800);
  };
  dlg.querySelector('[data-ow-mail-connect]').addEventListener('click', go);
  dlg.querySelector('#ow-mail-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

async function openFeedDialog() {
  const acc = _accounts || await fetchAccounts();
  const help = (acc && acc.feed_help) || [];
  const html = `
    <div class="ow-acct-lead">Paste a calendar's share link. OpenWings shows its events in your Calendar and refreshes them every 30 minutes.</div>
    <div class="ow-acct-warn"><strong>Read only.</strong> OpenWings and the assistant can see these events but cannot change them. A secret link works like a password: anyone who has it can see the calendar, so keep it to yourself.</div>
    <details class="ow-acct-details"><summary>Where do I find the link?</summary>
      <dl class="ow-acct-dl">${help.map((h) => `<dt>${esc(h.name)}</dt><dd>${esc(h.steps)}${h.url ? ` <a href="${esc(h.url)}" target="_blank" rel="noopener noreferrer">${esc(h.link || 'Steps')}</a>` : ''}</dd>`).join('')}</dl>
    </details>
    <label class="ow-acct-label" for="ow-feed-url">Calendar link</label>
    <input id="ow-feed-url" class="settings-input" type="url" autocomplete="off" spellcheck="false" placeholder="https://…/basic.ics or webcal://…">
    <label class="ow-acct-label" for="ow-feed-name">Name (optional)</label>
    <input id="ow-feed-name" class="settings-input" type="text" maxlength="80" placeholder="For example Family or Work">
    <div class="ow-acct-actions"><button type="button" class="admin-btn-add" data-ow-feed-add>Add calendar</button><button type="button" class="admin-btn-sm" data-ow-close>Cancel</button></div>
    <div class="adm-ep-inline-msg" data-ow-dialog-msg></div>`;
  const dlg = openDialog('ow-feed-dialog', 'Add a calendar by link', html);
  const msg = dlg.querySelector('[data-ow-dialog-msg]');
  const go = async () => {
    const url = dlg.querySelector('#ow-feed-url').value.trim();
    if (!url) return setMsg(msg, 'Paste the calendar link.', 'err');
    const b = dlg.querySelector('[data-ow-feed-add]');
    b.disabled = true;
    setMsg(msg, 'Loading the calendar…');
    const r = await postJson('/api/wings/accounts/calendar-link', { url, name: dlg.querySelector('#ow-feed-name').value.trim() });
    b.disabled = false;
    if (!r.ok) return setMsg(msg, r.data.message || r.data.detail || 'That link did not work.', 'err');
    setMsg(msg, `Added "${r.data.name}" with ${r.data.events} events. It is in your Calendar now.`, 'ok');
    try { window.dispatchEvent(new CustomEvent('ow-calendars-changed')); } catch (_) {}
    const host = document.getElementById('ow-connect-apps-card');
    if (host) renderAccounts(host);
    setTimeout(() => dlg.close(), 1800);
  };
  dlg.querySelector('[data-ow-feed-add]').addEventListener('click', go);
  dlg.querySelector('#ow-feed-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

async function openGoogleDialog() {
  const acc = _accounts || await fetchAccounts();
  const g = (acc && acc.google) || {};
  if (!g.configured) {
    if (acc && acc.admin) return openGoogleSetup();
    openDialog('ow-google-dialog', 'Sign in with Google', `<div class="ow-acct-lead">Sign in with Google needs a one-time setup by your OpenWings admin. Google charges for the review that a shared one-click Gmail needs, so each OpenWings uses its own free Google key.</div>
      <div class="ow-acct-lead">Until then: Gmail works with an app password, and any Google calendar can be added with Calendar by link (read only).</div>
      <div class="ow-acct-actions"><button type="button" class="admin-btn-sm" data-ow-close>Close</button></div>`);
    return undefined;
  }
  const connected = (g.links || []).length > 0;
  const html = `
    <div class="ow-acct-lead">Google's own sign-in page opens. Choose what OpenWings may use for you:</div>
    <label class="ow-acct-check"><input type="checkbox" data-ow-scope="mail" checked> Mail: read, sort and send your Gmail</label>
    <label class="ow-acct-check"><input type="checkbox" data-ow-scope="calendar" checked> Calendar: see and change your main Google calendar, add Meet links</label>
    <label class="ow-acct-check"><input type="checkbox" data-ow-scope="drive" checked> Drive: the assistant can find and read your files (read only)</label>
    <div class="ow-acct-lead">On Google's page, leave every box ticked. A box you untick there is not connected.</div>
    ${connected ? '<div class="ow-acct-lead">What is connected already stays connected. To remove it, use Disconnect on the card.</div>' : ''}
    <div class="ow-acct-actions"><button type="button" class="admin-btn-add" data-ow-google-go>Sign in with Google</button><button type="button" class="admin-btn-sm" data-ow-close>Cancel</button></div>
    <div class="adm-ep-inline-msg" data-ow-dialog-msg></div>
    <div class="ow-app-note">Google may say it has not verified this app: that is your own OpenWings' key. Choose Advanced, then continue. Only you can use what you connect here. Other Google calendars than your main one: add them with Calendar by link.</div>`;
  const dlg = openDialog('ow-google-dialog', 'Sign in with Google', html);
  const msg = dlg.querySelector('[data-ow-dialog-msg]');
  dlg.querySelector('[data-ow-google-go]').addEventListener('click', async () => {
    const pick = {};
    dlg.querySelectorAll('[data-ow-scope]').forEach((x) => { pick[x.dataset.owScope] = x.checked; });
    if (!pick.mail && !pick.calendar && !pick.drive) return setMsg(msg, 'Tick at least one.', 'err');
    setMsg(msg, 'Opening Google…');
    const r = await postJson('/api/wings/accounts/google/start', pick);
    if (!r.ok || !r.data.authorize_url) return setMsg(msg, r.data.message || 'Could not start the sign-in.', 'err');
    window.location.href = r.data.authorize_url;
    return undefined;
  });
  return undefined;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.cssText = 'position:fixed;left:0;top:0;opacity:0;';
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
  ta.remove(); return ok;
}

async function openGoogleSetup() {
  let info;
  try {
    const r = await fetch('/api/wings/accounts/google/setup', { credentials: 'same-origin' });
    info = r.ok ? await r.json() : null;
  } catch (_) { info = null; }
  if (!info) { uiModule.showError('Only an OpenWings admin can set up the Google key.'); return; }
  const c = info.console || {};
  const client = info.client || {};
  const link = (u, t) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t)}</a>`;
  const current = client.configured
    ? `<div class="ow-acct-warn">${client.source === 'env' ? 'Set in .env' : 'Saved here'}: Client ID ${esc(client.client_id)}${client.source === 'env' ? '. A key saved below takes its place.' : ''}
        ${client.source === 'stored' ? '<button type="button" class="admin-btn-sm" data-ow-google-forget style="margin-left:6px;">Remove</button>' : ''}</div>`
    : '';
  const html = `
    <div class="ow-acct-lead">About 10 minutes, once, for everyone on this OpenWings. It is free. Each step opens Google's console in a new tab; sign in with any Google account you control.</div>
    ${current}
    <ol class="ow-acct-steps ow-acct-wizard">
      <li><strong>Create a project.</strong> ${link(c.project, 'Open the new project page')}. Any name, for example OpenWings.</li>
      <li><strong>Turn on the APIs</strong> for that project. Press Enable on each: ${link(c.gmail_api, 'Gmail API')}, ${link(c.calendar_api, 'Google Calendar API')}, ${link(c.caldav_api, 'CalDAV API')}, ${link(c.drive_api, 'Google Drive API')}.</li>
      <li><strong>Set up the sign-in screen.</strong> ${link(c.branding, 'Branding')}: press Get started. App name: OpenWings. Support and contact email: yours. Audience: <strong>External</strong>. Agree to the policy, then Create.</li>
      <li><strong>Publish it.</strong> ${link(c.audience, 'Audience')}: press Publish app. While it says Testing, only people you list there as test users can sign in, and only for 7 days at a time. If Google asks for a home page and privacy policy link first, add them under Branding (your own page or this OpenWings address). After you publish, Google may say the app needs verification: you can ignore that for your own OpenWings (up to 100 people can sign in without it). It costs nothing.</li>
      <li><strong>Make the key.</strong> ${link(c.clients, 'Clients')}: Create client. Application type: <strong>Web application</strong>. Name: OpenWings. If the form shows "Use this client for an AI-powered agent", leave it unticked. Under Authorized redirect URIs press Add URI and paste exactly:
        <div class="ow-acct-copy"><code data-ow-uri>${esc(info.redirect_uri)}</code><button type="button" class="admin-btn-sm" data-ow-copy-uri>Copy</button></div>
        ${info.redirect_ok ? '' : `<div class="ow-acct-warn">${esc(info.redirect_rules || '')} Open OpenWings at such an address and open this guide again.</div>`}
        ${info.only_this_computer ? '<div class="ow-acct-warn">With this address, Sign in with Google only works in a browser on the OpenWings computer itself. For phones and other computers, give OpenWings an https address (setup.sh --domain), then open this guide there.</div>' : ''}
        ${info.current_uri ? `<div class="ow-acct-warn">OpenWings is now opened at another address. To sign in from here, use <code>${esc(info.current_uri)}</code> instead and add it in Google's console too. <button type="button" class="admin-btn-sm" data-ow-use-current style="margin-left:6px;">Use this address instead</button></div>` : ''}
        Then press Create.</li>
      <li><strong>Paste the key here.</strong> Google shows the Client secret only once, so copy it right away.</li>
    </ol>
    <label class="ow-acct-label" for="ow-g-id">Client ID</label>
    <input id="ow-g-id" class="settings-input" type="text" autocomplete="off" spellcheck="false" placeholder="…apps.googleusercontent.com">
    <label class="ow-acct-label" for="ow-g-secret">Client secret</label>
    <input id="ow-g-secret" class="settings-input" type="password" autocomplete="off" spellcheck="false" placeholder="Paste the client secret">
    <div class="ow-acct-actions"><button type="button" class="admin-btn-add" data-ow-g-save>Save</button><button type="button" class="admin-btn-sm" data-ow-close>Close</button></div>
    <div class="adm-ep-inline-msg" data-ow-dialog-msg></div>
    <div class="ow-app-note">Saved encrypted on this server and never shown again. A new key usually works within minutes; Google says it can take up to five hours. Steps follow ${link(c.guide, 'Google\'s guide')}.</div>`;
  const dlg = openDialog('ow-google-setup', 'Set up Sign in with Google', html, { wide: true });
  const msg = dlg.querySelector('[data-ow-dialog-msg]');
  dlg.querySelector('[data-ow-copy-uri]').addEventListener('click', async (e) => {
    const ok = await copyText(info.redirect_uri);
    e.target.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => { e.target.textContent = 'Copy'; }, 1600);
  });
  dlg.querySelector('[data-ow-use-current]')?.addEventListener('click', async () => {
    const r = await postJson('/api/wings/accounts/google/setup/redirect', {});
    if (!r.ok) return setMsg(msg, r.data.message || 'Could not change the address.', 'err');
    dlg.close({ keepFocus: true });
    await openGoogleSetup();
    const m = document.querySelector('#ow-google-setup [data-ow-dialog-msg]');
    setMsg(m, 'Address changed. Add it under Authorized redirect URIs in Google\'s console too.', 'ok');
    return undefined;
  });
  dlg.querySelector('[data-ow-google-forget]')?.addEventListener('click', async () => {
    const ok = await askConfirm('Remove the saved Google key? People who signed in with Google stay signed in until their access runs out.', { confirmText: 'Remove', danger: true });
    if (!ok) return;
    await postJson('/api/wings/accounts/google/setup', undefined, 'DELETE');
    dlg.close();
    const host = document.getElementById('ow-connect-apps-card');
    if (host) renderAccounts(host);
  });
  dlg.querySelector('[data-ow-g-save]').addEventListener('click', async () => {
    const client_id = dlg.querySelector('#ow-g-id').value.trim();
    const client_secret = dlg.querySelector('#ow-g-secret').value.trim();
    if (!client_id || !client_secret) return setMsg(msg, 'Paste both the Client ID and the Client secret.', 'err');
    const r = await postJson('/api/wings/accounts/google/setup', { client_id, client_secret });
    if (!r.ok) return setMsg(msg, r.data.message || 'Could not save.', 'err');
    dlg.querySelector('#ow-g-secret').value = '';
    setMsg(msg, 'Saved. Everyone here can now use Sign in with Google.', 'ok');
    const host = document.getElementById('ow-connect-apps-card');
    if (host) renderAccounts(host);
    return undefined;
  });
}

async function confirmRemove(text) {
  return askConfirm(text, { confirmText: 'Disconnect', danger: true });
}

async function onAccountsClick(card, e) {
  const t = (sel) => e.target.closest(sel);
  let el;
  if ((el = t('[data-ow-app-connect]'))) return connectApp(card, el.dataset.owAppConnect);
  if ((el = t('[data-ow-app-disconnect]'))) return disconnectApp(card, el.dataset.owAppDisconnect, el.dataset.owAppName || 'this app');
  if ((el = t('[data-ow-acct-open]'))) return openAccountCard(el.dataset.owAcctOpen);
  if (t('[data-ow-google-setup]')) return openGoogleSetup();
  if ((el = t('[data-ow-mail-remove]'))) {
    if (!await confirmRemove(`Disconnect ${el.dataset.owName}? OpenWings forgets the password and stops reading this mailbox.`)) return undefined;
    await postJson(`/api/wings/accounts/mail/${encodeURIComponent(el.dataset.owMailRemove)}`, undefined, 'DELETE');
    return renderAccounts(card);
  }
  if ((el = t('[data-ow-google-remove]'))) {
    if (!await confirmRemove(`Disconnect ${el.dataset.owName}? OpenWings signs out of Google and removes this account's mail and calendars from OpenWings.`)) return undefined;
    await postJson(`/api/wings/accounts/google/${encodeURIComponent(el.dataset.owGoogleRemove)}`, undefined, 'DELETE');
    return renderAccounts(card);
  }
  if ((el = t('[data-ow-feed-refresh]'))) {
    el.disabled = true; el.textContent = 'Refreshing…';
    const r = await postJson(`/api/wings/accounts/calendar-link/${encodeURIComponent(el.dataset.owFeedRefresh)}/refresh`);
    if (!r.ok) uiModule.showError(r.data.message || 'Could not refresh that calendar.');
    else uiModule.showToast(`Calendar refreshed: ${r.data.events} events.`);
    try { window.dispatchEvent(new CustomEvent('ow-calendars-changed')); } catch (_) {}
    return renderAccounts(card);
  }
  if ((el = t('[data-ow-feed-remove]'))) {
    const ok = await askConfirm(`Remove "${el.dataset.owName}"? Its events leave your Calendar.`, { confirmText: 'Remove', danger: true });
    if (!ok) return undefined;
    await postJson(`/api/wings/accounts/calendar-link/${encodeURIComponent(el.dataset.owFeedRemove)}`, undefined, 'DELETE');
    try { window.dispatchEvent(new CustomEvent('ow-calendars-changed')); } catch (_) {}
    return renderAccounts(card);
  }
  return undefined;
}

function openAccountCard(id) {
  const c = ACCOUNT_CARDS.find((x) => x.id === id);
  if (c && c.open) return c.open(c);
  if (c && c.kind === 'mail') return openMailDialog(c.provider);
  if (id === 'google') return openGoogleDialog();
  if (id === 'calendar-link') return openFeedDialog();
  return undefined;
}

// Other screens (Email accounts, Calendar settings) point here: open
// Settings > Integrations at this card, and its dialog when asked.
async function goToAccounts(id, { dialog = true } = {}) {
  try { settingsModule.open('integrations'); } catch (_) {}
  const card = await waitFor(() => document.getElementById('ow-connect-apps-card'), 6000);
  if (!card) return;
  const el = await waitFor(() => card.querySelector(`[data-ow-acct="${CSS.escape(id || 'gmail')}"]`), 4000);
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.add('ow-flash');
    setTimeout(() => el.classList.remove('ow-flash'), 1600);
  }
  if (dialog && id) openAccountCard(id);
}
window.owGoToAccounts = goToAccounts;

async function renderSettingsAppsCard() {
  const panel = document.querySelector('[data-settings-panel="integrations"]');
  if (!panel || document.getElementById('ow-connect-apps-card')) return;
  const card = document.createElement('div');
  card.className = 'admin-card';
  card.id = 'ow-connect-apps-card';
  card.innerHTML = `<h2>${APP_ICON.replace('aria-hidden="true"', 'class="ow-card-ico" aria-hidden="true"')}Connect your accounts</h2>
    <div class="admin-toggle-sub" style="margin-bottom:8px">Each person connects their own mail, calendars and files; the assistant uses them only for that person. Work apps are connected once by an admin for everyone.</div>
    <div class="ow-acct-groups"><div class="ow-app-note">Loading…</div></div>`;
  panel.insertBefore(card, panel.firstChild);
  card.addEventListener('click', (e) => { onAccountsClick(card, e); });
  await renderAccounts(card);
  // Refresh whenever the Integrations panel is shown again.
  new MutationObserver(() => {
    if (!panel.classList.contains('hidden')) renderAccounts(card);
  }).observe(panel, { attributes: true, attributeFilter: ['class'] });
}

document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest('[data-ow-open-accounts]');
  if (!el) return;
  e.preventDefault();
  goToAccounts(el.dataset.owOpenAccounts, { dialog: el.dataset.owOpenDialog !== '0' });
});

// ------------------------------------------------------------ return notes

function handleChatgptReturn() {
  const sp = new URLSearchParams(window.location.search);
  if (!sp.has('ow_chatgpt') && !sp.has('ow_open')) return;
  const res = sp.get('ow_chatgpt');
  const open = sp.get('ow_open');
  sp.delete('ow_chatgpt');
  sp.delete('ow_open');
  const rest = sp.toString();
  window.history.replaceState(null, '', window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash);
  setTimeout(async () => {
    if (res === 'connected') {
      try { await fetch('/api/models?refresh=true', { credentials: 'same-origin' }); } catch (_) {}
      const st = await fetchStatus(true);
      uiModule.showToast('ChatGPT is connected. Pick a ChatGPT plan model and say hello.', { duration: 6000 });
      if (st.chatgpt && st.chatgpt.state === 'connected' && !st.chatgpt.welcomed) showChatgptWelcome();
    } else if (res) {
      uiModule.showError(CHATGPT_ERRORS[res] || 'ChatGPT could not be connected. Please try again.');
    }
    if (open === 'chatgpt' || (res && res !== 'connected')) {
      const st = await fetchStatus();
      if (st.admin && st.model_endpoints !== 0) {
        settingsModule.open('services');
        const c = await waitFor(() => document.querySelector('#ow-connect-ai-card [data-ow-choice="chatgpt"]'));
        if (c) c.scrollIntoView({ block: 'center' });
      }
    }
  }, 600);
}

// Back from Sign in with Google (wings/accounts_google.py callback):
// ?ow_accounts=google (with ow_accounts_note=missing_<parts> when Google did
// not grant something) or ?ow_accounts_error=<code>. Open the Google card
// and say plainly what happened.
function handleAccountsReturn() {
  const sp = new URLSearchParams(window.location.search);
  if (!sp.has('ow_accounts') && !sp.has('ow_accounts_error') && !sp.has('ow_accounts_note')) return;
  const ok = sp.get('ow_accounts');
  const err = sp.get('ow_accounts_error');
  const note = sp.get('ow_accounts_note') || '';
  ['ow_accounts', 'ow_accounts_error', 'ow_accounts_note'].forEach((k) => sp.delete(k));
  const rest = sp.toString();
  window.history.replaceState(null, '', window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash);
  setTimeout(async () => {
    goToAccounts('google', { dialog: false });
    if (err || ok !== 'google') {
      uiModule.showError(ACCOUNT_ERRORS[err] || 'Sign in with Google did not finish. Please try again.');
      return;
    }
    const acc = await fetchAccounts();
    const links = (acc && acc.google && acc.google.links) || [];
    const parts = (l) => ['mail', 'calendar', 'drive'].filter((k) => l[k]).map((k) => FEATURE_NAMES[k]);
    const what = links.map((l) => `${l.account} (${listWords(parts(l)) || 'nothing yet'})`).join('; ');
    const missing = note.startsWith('missing_') ? note.slice(8).split('_').filter((k) => FEATURE_NAMES[k]).map((k) => FEATURE_NAMES[k]) : [];
    if (missing.length) {
      uiModule.showError(`Signed in, but Google did not give OpenWings access to ${listWords(missing)}. Press Sign in again or add more, and leave ${missing.length > 1 ? 'those boxes' : 'that box'} ticked on Google's page.`);
    } else {
      uiModule.showToast(`Signed in with Google${what ? `: ${what}` : ''}. Mail and calendars appear in a minute.`, { duration: 7000 });
    }
    if (links.some((l) => l.calendar)) { try { window.dispatchEvent(new CustomEvent('ow-calendars-changed')); } catch (_) {} }
  }, 600);
}

function handleReturn() {
  handleChatgptReturn();
  handleAccountsReturn();
  const sp = new URLSearchParams(window.location.search);
  if (!sp.has('ow_connect') && !sp.has('ow_connect_error')) return;
  const ok = sp.get('ow_connect');
  const err = sp.get('ow_connect_error');
  sp.delete('ow_connect');
  sp.delete('ow_connect_error');
  const rest = sp.toString();
  window.history.replaceState(null, '', window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash);
  setTimeout(() => {
    if (ok === 'openrouter') uiModule.showToast('OpenRouter is connected for everyone on this OpenWings. Pick a model and say hello.', { duration: 6000 });
    else if (err) uiModule.showError(OPENROUTER_ERRORS[err] || 'OpenRouter could not be connected.');
  }, 600);
}

async function init() {
  injectStyle();
  handleReturn();
  await fetchStatus();
  renderWelcome();
  renderSettingsAiCard();
  renderSettingsAppsCard();
  watchPlanChip();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();

export { fetchStatus };
