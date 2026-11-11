#!/usr/bin/env python3
# Build overlay/app/wings/brand/tokens.json (W3C design-token shape) from
# tokens.css, the source of truth, so the two never drift.
#   python3 scripts/dev/brand/lichen/tokens_json.py
import json, os, re, sys
BRAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..', 'overlay', 'app', 'wings', 'brand')
src = open(os.path.join(BRAND, 'tokens.css'), encoding='utf-8').read()
main = src[src.index(':root[data-ow-skin="ow-sage"] {'):src.index('/* the spring as a real curve')]
supports = src[src.index('@supports'):src.index('/* Reduced motion')]
reduced = src[src.index('@media (prefers-reduced-motion'):]
def decls(block):
    block = re.sub(r'/\*.*?\*/', '', block, flags=re.S)
    return re.findall(r'--([a-z0-9-]+):\s*([^;]+);', block)
comments = {}
for name, val, com in re.findall(r'--([a-z0-9-]+):\s*([^;]+);[ \t]*/\*\s*(.*?)\s*\*/', main):
    comments[name] = com
props = decls(main)
spring = dict(decls(supports))['ease-spring']
red = dict(decls(reduced))

def alias(v):
    return re.sub(r'var\(--([a-z0-9-]+)\)', lambda m: '{' + path_of(m.group(1)) + '}', v)

GROUPS = [
    ('sg-', 'primitive.color', 'color'),
    ('color-', 'color', 'color'),
    ('font-size-', 'font.size', 'dimension'),
    ('font-weight-', 'font.weight', 'fontWeight'),
    ('font-', 'font.family', 'fontFamily'),
    ('leading-', 'font.leading', 'number'),
    ('tracking-', 'font.tracking', 'dimension'),
    ('measure', 'font.measure', 'dimension'),
    ('space-', 'space', 'dimension'),
    ('grid-', 'grid', 'dimension'),
    ('radius-', 'radius', 'dimension'),
    ('border-', 'border', 'border'),
    ('shadow-', 'shadow', 'shadow'),
    ('ease-', 'motion.easing', 'cubicBezier'),
    ('duration-', 'motion.duration', 'duration'),
    ('stagger-', 'motion.stagger', 'duration'),
    ('rise-', 'motion.rise', 'dimension'),
    ('shift-', 'motion.shift', 'dimension'),
    ('hover-', 'motion.scale', 'number'),
    ('press-', 'motion.scale', 'number'),
    ('pop-', 'motion.scale', 'number'),
    ('exit-', 'motion.scale', 'number'),
    ('lift-', 'motion.lift', 'dimension'),
    ('delay-', 'motion.delay', 'duration'),
    ('z-', 'z', 'number'),
]
def classify(name):
    for pre, grp, typ in GROUPS:
        if name.startswith(pre):
            leaf = name[len(pre):] if name != pre.rstrip('-') else name
            if pre in ('hover-', 'press-', 'pop-', 'exit-'): leaf = pre.rstrip('-')
            if pre == 'measure': leaf = 'measure'; grp = 'font'
            return grp, leaf or name, typ
    return 'component', name, 'string'
def path_of(name):
    g, leaf, _ = classify(name)
    return g + '.' + leaf
out = {"$description": "OpenWings design tokens for the Lichen look (theme key ow-sage). Generated from tokens.css, which is the source of truth; values are identical. Aliases use {group.token}.",
       "$extensions": {"openwings": {"scope": ':root[data-ow-skin="ow-sage"]', "mode": "one mode for everyone (no dark variant)"}}}
def put(path, obj):
    cur = out
    parts = path.split('.')
    for p_ in parts[:-1]: cur = cur.setdefault(p_, {})
    cur[parts[-1]] = obj
for name, val in props:
    val = val.strip()
    g, leaf, typ = classify(name)
    if g == 'grid' and leaf == 'columns': typ = 'number'
    if g == 'motion.easing' and not val.startswith('cubic-bezier'): typ = 'string'
    if g == 'motion.stagger' and leaf == 'max': typ = 'number'
    if g == 'font.family':
        v = [x.strip().strip("'") for x in val.split(',')]
    elif typ == 'cubicBezier':
        v = [float(x) for x in re.findall(r'[\d.]+', val)]
    else:
        v = alias(val)
    if typ in ('number', 'fontWeight') and isinstance(v, str) and re.fullmatch(r'-?[\d.]+', v):
        v = float(v) if '.' in v else int(v)
    tok = {"$value": v, "$type": typ}
    if name in comments: tok["$description"] = comments[name]
    if name == 'ease-spring':
        tok["$type"] = "string"
        tok["$value"] = spring
        fb = val.strip()
        tok["$description"] = "underdamped spring (bounce 0.1, response 0.36s, sampled over 400ms) as CSS linear(), for the one lead title of a page load; fallback " + fb + " where linear() is unsupported"
        tok["$extensions"] = {"openwings": {"fallback": fb}}
    if name in red:
        tok.setdefault("$extensions", {}).setdefault("openwings", {})["prefersReducedMotion"] = red[name].strip()
    put((g + '.' + leaf), tok)
json.dump(out, open(os.path.join(BRAND, 'tokens.json'), 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
print('tokens', len(props))
