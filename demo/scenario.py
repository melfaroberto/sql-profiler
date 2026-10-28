"""Plays a series of user actions on the Mini Shop app, recording each one as a profiler session,
like you would do by hand with the Start/Stop recording button. Then exports the result.

    python demo/scenario.py                                   # app on :8090, profiler dashboard on :8780
    python demo/scenario.py --profiler http://127.0.0.1:8780 --app http://127.0.0.1:8090 -o report.md
"""
import argparse
import asyncio
import re
import time

import aiohttp


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--app", default="http://127.0.0.1:8090")
    p.add_argument("--profiler", default="http://127.0.0.1:8780")
    p.add_argument("-o", "--output", default="demo/scenario-report.md")
    a = p.parse_args()

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as app, aiohttp.ClientSession() as prof:
        async def action(label, steps):
            """Start recording, perform the steps (HTTP calls on the app), stop recording."""
            r = await prof.post(f"{a.profiler}/api/recording/start", json={"label": label})
            session = await r.json()
            await asyncio.sleep(0.2)
            result = None
            try:
                for method, path, data in steps:
                    async with app.request(method, a.app + path, data=data) as resp:
                        result = (resp.status, str(resp.url), await resp.text())
                        if resp.status >= 500:
                            raise SystemExit(f"the app failed on {method} {path} (HTTP {resp.status}), see its console")
            finally:
                await asyncio.sleep(0.5)         # let the last statements reach the profiler
                await prof.post(f"{a.profiler}/api/recording/stop")
            print(f"  ✓ {label:<38} → {result[1].replace(a.app, '') or '/'}")
            return session, result

        for name, base, path in (("Mini Shop app", a.app, "/login"), ("Profiler", a.profiler, "/api/status")):
            try:
                async with app.get(base + path) as resp:
                    resp.raise_for_status()
            except aiohttp.ClientError:
                raise SystemExit(f"{name} not reachable at {base}: start the demo first with demo/run_demo.sh")
        print("Playing the scenario…")
        await action("Failed login (wrong password)", [("POST", "/login", {"username": "enrico", "password": "nope"})])
        await action("Login as enrico", [("POST", "/login", {"username": "enrico", "password": "enrico"})])
        await action("Search customers in Roma", [("GET", "/customers?q=Roma", None)])
        await action("Open customer card", [("GET", "/customers/1", None)])
        _, (_, url, _) = await action("Create customer", [("POST", "/customers/new",
                                                           {"name": "Blu Sas", "email": "blu@example.com", "city": "Bari"})])
        new_customer = url.rsplit("/", 1)[-1]
        _, (_, url, _) = await action("Save order (2 products)", [
            ("GET", f"/orders/new?customer={new_customer}", None),
            ("POST", "/orders/new", {"customer": new_customer, "qty_1": "2", "qty_3": "10"})])
        order_id = re.search(r"/orders/(\d+)", url).group(1)
        await action("Save order — not enough stock", [("POST", "/orders/new", {"customer": "1", "qty_5": "999"})])
        await action("Cancel order", [("POST", f"/orders/{order_id}/cancel", None)])
        await action("Open reports", [("GET", "/reports", None)])
        await action("Logout", [("GET", "/logout", None)])

        sessions = (await (await prof.get(f"{a.profiler}/api/sessions")).json())["sessions"]
        mine = [s for s in sessions if s["started_at"] > time.time() - 600][:10]
        print("\nRecorded sessions (statements without driver noise / errors):")
        for s in reversed(mine):
            print(f"  #{s['id']:<4} {s['label']:<38} {s['relevant']:>3} statements  {s['errors']} errors")
        ids = ",".join(str(s["id"]) for s in reversed(mine))
        md = await (await prof.get(f"{a.profiler}/api/export?sessions={ids}&format=md")).text()
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\nMarkdown export written to {a.output}")
        print(f"Open {a.profiler} to explore the sessions in the dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
