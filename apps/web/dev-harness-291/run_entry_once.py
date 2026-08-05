import sys, http.server, socketserver, threading, time
from playwright.sync_api import sync_playwright
DIST = sys.argv[1]; PAGE = sys.argv[2]; WAIT = float(sys.argv[3]) if len(sys.argv) > 3 else 10
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=DIST, **kw)
    def log_message(self, *a): pass
with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        msgs = []
        page.on("console", lambda m: msgs.append(m.text))
        page.on("pageerror", lambda e: msgs.append("PAGEERROR: " + str(e)))
        page.goto(f"http://127.0.0.1:{port}/{PAGE}")
        time.sleep(WAIT)
        print(page.locator("#out").inner_text())
        print("--- console ---")
        for m in msgs[-20:]: print(m)
        browser.close()
    httpd.shutdown()
