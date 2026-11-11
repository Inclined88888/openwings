"""Browser actions checked directly, without a model in between: batch,
hover, the pointer guard (web components, closed shadow roots), canvas
clicks, drag and drop and the pay button handoff. Runs inside the app
container against the browser container and the test site
(tests/e2e/run.sh starts both, with WINGS_BROWSER_ALLOW_PRIVATE=1):

  docker exec -i -e WINGS_BROWSER_TOKEN="$(grep '^WINGS_BROWSER_TOKEN=' .env | cut -d= -f2-)" \
    wings-openwings-1 python -u - < tests/e2e/browser_actions.py

The chat driven checks in wings_e2e.py cover the same actions through the
model and the request authority; this one does not depend on the model
connection, which is unreliable in an offline sandbox.
"""
import asyncio, os, re, sys
sys.path.insert(0, "/app")
from wings import keyholder
keyholder.BROWSER_TOKEN = os.environ["WINGS_BROWSER_TOKEN"]
from wings import browser as B
site = "http://testsite:8000"
fails = 0
def check(name, ok, out):
    global fails
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + out[-400:].replace("\n", " | ") + "]"))
    fails += 0 if ok else 1
async def call(args):
    _, result = await B.run(args, owner="admin", session_id="direct-check")
    return str(result.get("output") or "") + ("" if not result.get("error") else "\nERROR: " + str(result.get("error")))
async def main():
    await call({"action": "open", "url": f"{site}/statements1.html"})
    out = await call({"action": "batch", "pages": {"next": "Next page", "max": 10}, "steps": [{"action": "read"}]})
    check("batch reads every page of a paginated table", all(f"Statement {n}-B" in out for n in (1, 2, 3)) and "3 page(s)" in out, out)
    out = await call({"action": "batch", "each": ["1", "2"], "steps": [
        {"action": "open", "url": f"{site}/statements{{item}}.html"},
        {"action": "click", "find": "Open details for page {item}"}, {"action": "read"}]})
    check("batch repeats steps per item", "Details of statement page 1" in out and "Details of statement page 2" in out, out)
    out = await call({"action": "batch", "steps": [{"action": "open", "url": f"{site}/checkout.html"}, {"action": "read"},
                                                   {"action": "click", "find": "Place order"}, {"action": "read"}]})
    check("a batch stops for the person at an order button", "PAUSED" in out and "Batch stopped here" in out, out)
    out = await call({"action": "read"})
    check("the order was not placed", "ORDER PLACED" not in out, out)
    out = await call({"action": "batch", "steps": [{"action": "evaluate", "script": "1"}]})
    check("a batch cannot run page JavaScript", "cannot run in a batch" in out, out)
    out = await call({"action": "open", "url": f"{site}/menu.html"})
    hidden_before = "Hidden monthly report" in out
    out = await call({"action": "hover", "find": "Reports menu"})
    check("hover opens a menu that appears only under the pointer", not hidden_before and "Hidden monthly report" in out, out)
    await call({"action": "open", "url": f"{site}/shadow.html"})
    for name, x, y in (("an icon inside a labelled Delete button", 40, 30), ("a labelled icon component", 40, 120),
                       ("a closed shadow root's Place order button", 60, 320)):
        out = await call({"action": "click", "x": x, "y": y})
        check(f"a pointer click on {name} is handed to the person", "PAUSED" in out, out)
    out = await call({"action": "read"})
    check("nothing was deleted or ordered", "DELETED" not in out and "ORDERED" not in out, out)
    out = await call({"action": "open", "url": f"{site}/canvas.html"})
    m = re.search(r"canvas[^\n]*x=(\d+)\.\.(\d+) y=(\d+)\.\.(\d+)", out)
    if m:
        out = await call({"action": "click", "x": int(m.group(1)) + 25, "y": int(m.group(3)) + 41})
        out = await call({"action": "read"})
        hit = re.search(r"Canvas clicked at (\d+),(\d+)", out)
        # The outline rounds the canvas position to whole pixels: allow 1 px.
        check("a plain canvas click still works", bool(hit) and abs(int(hit.group(1)) - 25) <= 1
              and abs(int(hit.group(2)) - 41) <= 1, out)
    else:
        check("the canvas outline gives its area", False, out)
    out = await call({"action": "drag", "find": "Drag me", "text": "Drop here"})
    out = await call({"action": "read"})
    check("drag and drop onto a drop zone still works", "Dropped on target" in out, out)
    out = await call({"action": "click", "find": "Pay now"})
    check("a pay button is still handed to the person", "PAUSED" in out, out)
    print(f"\n{fails} failure(s)")
asyncio.run(main())
