const { chromium } = require('playwright');
const fs = require('fs');
const B = process.argv[2], OUT = process.argv[3];
(async () => {
  const b = await chromium.launch();
  const shot = async (html, w, h, path, transparent = false) => {
    const p = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
    await p.setContent(html, { waitUntil: 'load' });
    await p.evaluate(() => document.fonts.ready);
    await p.screenshot({ path, omitBackground: transparent });
    await p.close();
  };
  const svg = (name, w, h) => fs.readFileSync(`${B}/${name}`, 'utf8').replace('<svg ', `<svg width="${w}" height="${h}" `);
  const page = (inner, bg = 'transparent') => `<html><body style="margin:0;background:${bg}">${inner}</body></html>`;
  for (const [n, s] of [['icon-192.png', 192], ['icon-512.png', 512], ['apple-touch-icon.png', 180],
                        ['favicon-16.png', 16], ['favicon-32.png', 32], ['favicon-48.png', 48]]) {
    await shot(page(svg('openwings-tile.svg', s, s)), s, s, `${OUT}/${n}`, true);
  }
  await shot(page(svg('openwings-maskable.svg', 512, 512)), 512, 512, `${OUT}/icon-maskable-512.png`);
  await shot(page(svg('openwings-mark.svg', 512, 512)), 512, 512, `${OUT}/openwings-mark.png`);
  // GitHub social preview, 1280 x 640.
  const social = `<html><body style="margin:0;width:1280px;height:640px;background:#fff;display:flex;align-items:center;justify-content:center;gap:56px;font-family:'Inter Display','Inter',sans-serif">
    ${svg('openwings-wings.svg', 340, 340)}
    <div><div style="font-size:124px;font-weight:800;letter-spacing:-3px;color:#000;line-height:1">OpenWings</div>
    <div style="font-size:38px;font-weight:500;color:#333;margin-top:22px">Your self hosted AI workspace</div></div></body></html>`;
  await shot(social, 1280, 640, `${OUT}/openwings-social.png`);
  await b.close();
})();
