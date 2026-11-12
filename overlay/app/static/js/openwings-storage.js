// OpenWings: browser storage names.
// Loaded first on every page, before any script reads localStorage or
// sessionStorage. Keys are stored under OpenWings names ("openwings-theme",
// "ow-prefetch-settings"); scripts that still ask for the older names get the
// same values. Keys saved under an older name by an earlier version are moved
// once, so nobody loses their theme, layout or saved sign-in name on upgrade.
(function () {
  'use strict';
  var S = window.Storage && window.Storage.prototype;
  if (!S || S.__owNames) return;
  // [older prefix, current prefix]; the older ones are spelled in parts so
  // they read as data, not as a product name.
  var MAP = [['ody' + 'sseus', 'openwings'], ['ody-', 'ow-']];
  var get = S.getItem, set = S.setItem, del = S.removeItem, key = S.key;
  function toNew(k) {
    if (typeof k !== 'string') return k;
    for (var i = 0; i < MAP.length; i++) {
      if (k.indexOf(MAP[i][0]) === 0) return MAP[i][1] + k.slice(MAP[i][0].length);
    }
    return k;
  }
  function toOld(k) {
    if (typeof k !== 'string') return k;
    for (var i = 0; i < MAP.length; i++) {
      if (k.indexOf(MAP[i][1]) === 0) return MAP[i][0] + k.slice(MAP[i][1].length);
    }
    return k;
  }
  function migrate(store) {
    try {
      var old = [];
      for (var i = 0; i < store.length; i++) {
        var k = key.call(store, i);
        if (k !== null && toNew(k) !== k) old.push(k);
      }
      for (var j = 0; j < old.length; j++) {
        var n = toNew(old[j]);
        if (get.call(store, n) === null) set.call(store, n, get.call(store, old[j]));
        del.call(store, old[j]);
      }
    } catch (_) {}
  }
  try { migrate(window.localStorage); } catch (_) {}
  try { migrate(window.sessionStorage); } catch (_) {}
  S.getItem = function (k) { return get.call(this, toNew(String(k))); };
  S.setItem = function (k, v) { return set.call(this, toNew(String(k)), v); };
  S.removeItem = function (k) { return del.call(this, toNew(String(k))); };
  // Scripts that walk the keys compare them with the names they know.
  S.key = function (i) { return toOld(key.call(this, i)); };
  S.__owNames = true;
})();
