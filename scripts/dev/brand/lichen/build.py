#!/usr/bin/env python3
"""Build the Lichen look (theme key ow-sage).

  python3 scripts/dev/brand/lichen/build.py

looks/sage.css is generated: edit sage.src.css here, where @@ stands for the
look's scope (:root[data-ow-skin="ow-sage"]) and __WINGS__ for the wings mark
as a data URI (wings-uri.txt), then run this. It also regenerates tokens.json
from tokens.css (tokens_json.py). Then rebuild the image."""
import os
import runpy

HERE = os.path.dirname(os.path.abspath(__file__))
BRAND = os.path.join(HERE, '..', '..', '..', '..', 'overlay', 'app', 'wings', 'brand')
src = open(os.path.join(HERE, 'sage.src.css'), encoding='utf-8').read()
wings = open(os.path.join(HERE, 'wings-uri.txt'), encoding='utf-8').read().strip()
css = src.replace('@@', ':root[data-ow-skin="ow-sage"]').replace('__WINGS__', wings)
with open(os.path.join(BRAND, 'looks', 'sage.css'), 'w', encoding='utf-8') as fh:
    fh.write(css)
print('looks/sage.css', len(css), 'bytes')
runpy.run_path(os.path.join(HERE, 'tokens_json.py'), run_name='__main__')
