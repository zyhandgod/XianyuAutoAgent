# Test entrypoints

Full suite, from repository root:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest discover -s tests -v
node --check admin_static/app.js
node --check admin_static/auth.js
git diff --check
```

Server browser integration requires Linux, Xvfb and installed Playwright Chromium. Run under the card-console service account: start a ServerBrowser, wait for ready, request a JPEG frame, send a harmless pointer/wheel event, close, verify its process exits. Use a temporary script guarded by `if __name__ == '__main__'` because the browser uses multiprocessing spawn. Do not log cookies/screenshots/challenge URLs or solve platform challenges automatically.
