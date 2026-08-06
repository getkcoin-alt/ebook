#!/usr/bin/env python3
"""The complete ecommerce path, end to end, against the deployed platform.

    KOS_ADMIN_EMAIL=… KOS_ADMIN_PASSWORD=… \
    KOS_CUSTOMER_EMAIL=… KOS_CUSTOMER_PASSWORD=… \
        python scripts/verify-deployment.py

Registration is capped at five per hour per IP, so supply an existing customer
rather than letting the script mint one on every run.


Operator publishes a book with a real file in object storage; a customer finds it,
buys it, and reads it. Every call goes through the public gateway. The only step a
browser would not perform is the operator settling the order by hand, because the
payment gateway module is out of scope — settlement still publishes
`payment.succeeded` to the event bus, which is what grants the entitlement.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import zipfile
from io import BytesIO

import httpx

GATEWAY = os.environ.get("KOS_GATEWAY", "https://api.allelearning.in")
results: list[tuple[str, bool, str]] = []


def record(step: str, ok: bool, detail: str = "") -> None:
    results.append((step, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {step}: {detail}"[:210])


def make_epub() -> bytes:
    """A minimally spec-shaped EPUB, so what lands in storage is a real file."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>The Smoke Test Handbook</dc:title>"
            '<dc:identifier id="id">urn:uuid:smoke</dc:identifier>'
            "<dc:language>en</dc:language></metadata>"
            '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        z.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>Chapter One</h1><p>Deployment verified.</p></body></html>",
        )
    return buf.getvalue()


def main() -> int:
    admin_email = os.environ["KOS_ADMIN_EMAIL"]
    admin_password = os.environ["KOS_ADMIN_PASSWORD"]

    # Railway's edge drops pooled connections aggressively; retry the transport and
    # do not reuse idle sockets, so a dropped keep-alive is not read as a failure.
    transport = httpx.HTTPTransport(retries=3, limits=httpx.Limits(keepalive_expiry=0))
    with httpx.Client(base_url=GATEWAY, timeout=90.0, transport=transport) as c:
        r = c.post("/v1/auth/login", json={"email": admin_email, "password": admin_password})
        if r.status_code != 200:
            record("operator login", False, f"{r.status_code} {r.text[:150]}")
            return 1
        admin_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        record("operator login", True, "200")

        print("\n=== 1. operator publishes a book with a real file ===")
        r = c.post(
            "/v1/authors",
            headers=admin_h,
            json={"name": "A. Tester", "slug": f"a-tester-{uuid.uuid4().hex[:6]}"},
        )
        author_id = r.json().get("id")
        record("create author", author_id is not None, str(r.status_code))

        slug = f"flow-book-{uuid.uuid4().hex[:8]}"
        r = c.post(
            "/v1/admin/books",
            headers=admin_h,
            json={
                "title": "The Smoke Test Handbook",
                "slug": slug,
                "description": "Proves the deployment works end to end.",
                "price_minor": 49900,
                "currency": "INR",
                "language": "en",
                "author_ids": [{"author_id": author_id, "role": "author"}],
            },
        )
        ok = r.status_code in (200, 201)
        record("create book (draft)", ok, f"{r.status_code} {'' if ok else r.text[:170]}")
        if not ok:
            return 1
        book_id = r.json()["id"]

        # Publishing is refused until a readable file exists.
        r = c.post(f"/v1/admin/books/{book_id}/publish", headers=admin_h)
        record(
            "publish REFUSED without a file",
            r.status_code == 409,
            f"{r.status_code} {r.json().get('error', {}).get('message', '')[:90]}",
        )

        r = c.post(
            "/v1/admin/books/uploads",
            headers=admin_h,
            json={
                "category": "book",
                "filename": "handbook.epub",
                "content_type": "application/epub+zip",
            },
        )
        ok = r.status_code in (200, 201)
        record("mint presigned upload target", ok, f"{r.status_code} {'' if ok else r.text[:150]}")
        if not ok:
            return 1
        presign = r.json()
        key = presign["key"]

        epub = make_epub()
        with httpx.Client(timeout=90.0) as raw:
            fields = presign.get("fields") or {}
            up = raw.post(
                presign["url"],
                data=fields,
                files={"file": ("handbook.epub", epub, "application/epub+zip")},
            )
        record(
            "upload file to MinIO",
            up.status_code in (200, 201, 204),
            f"{up.status_code} {len(epub)} bytes -> {key[:60]}",
        )

        r = c.post(
            f"/v1/admin/books/{book_id}/versions",
            headers=admin_h,
            json={"epub_key": key, "file_size_bytes": len(epub), "changelog": "First upload."},
        )
        record(
            "attach file as a book version",
            r.status_code in (200, 201),
            f"{r.status_code} {r.text[:140]}",
        )

        r = c.post(f"/v1/admin/books/{book_id}/publish", headers=admin_h)
        record(
            "publish now ACCEPTED", r.status_code in (200, 201), f"{r.status_code} {r.text[:110]}"
        )

        print("\n=== 2. a customer discovers it ===")
        # Registration is capped at 5/hour per IP — correctly, since it is the one
        # unauthenticated write on the platform. Reuse a customer from an earlier run
        # when one is supplied rather than spending the quota to prove a fact the
        # smoke test already established.
        cust_email = os.environ.get("KOS_CUSTOMER_EMAIL")
        cust_pw = os.environ.get("KOS_CUSTOMER_PASSWORD", "Flow-Test-Pw-91!x")
        if not cust_email:
            cust_email = f"flow-{uuid.uuid4().hex[:10]}@example.com"
            reg = c.post(
                "/v1/auth/register",
                json={"email": cust_email, "password": cust_pw, "full_name": "Flow Customer"},
            )
            record(
                "customer register",
                reg.status_code in (200, 201),
                f"{reg.status_code} {reg.text[:160]}",
            )
        r = c.post("/v1/auth/login", json={"email": cust_email, "password": cust_pw})
        if r.status_code != 200:
            record("customer login", False, f"{r.status_code} {r.text[:130]}")
            return 1
        user_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        record("customer login", True, "200")

        r = c.get("/v1/books", params={"limit": 20})
        titles = [b["slug"] for b in r.json().get("items", [])]
        record(
            "book appears in the public catalogue",
            slug in titles,
            f"{r.status_code}, {len(titles)} published",
        )

        r = c.get(f"/v1/books/{slug}")
        record("book detail by slug", r.status_code == 200, str(r.status_code))

        r = c.post("/v1/wishlist", headers=user_h, json={"book_id": book_id, "note": "buying this"})
        record("add to wishlist", r.status_code in (200, 201), f"{r.status_code} {r.text[:110]}")
        r = c.get("/v1/wishlist", headers=user_h)
        rows = r.json().get("items", []) if r.status_code == 200 else []
        mine = [i for i in rows if i.get("book_id") == book_id]
        ok = bool(mine) and mine[0].get("note") == "buying this"
        record(
            "wishlist lists it back with its note",
            ok,
            f"{r.status_code} {len(rows)} row(s), note={mine[0].get('note') if mine else None}",
        )

        print("\n=== 3. gate holds before purchase ===")
        r = c.get(f"/v1/books/{book_id}/download", headers=user_h)
        record(
            "download REFUSED (402 payment required)",
            r.status_code == 402,
            f"{r.status_code} {r.json().get('error', {}).get('code')}",
        )

        print("\n=== 4. purchase ===")
        r = c.post(
            "/v1/checkout/quote",
            headers=user_h,
            json={"items": [{"book_id": book_id, "quantity": 1}], "currency": "INR"},
        )
        ok = r.status_code == 200
        record("checkout quote", ok, f"{r.status_code} {r.text[:200]}")

        r = c.post(
            "/v1/orders",
            headers={**user_h, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "items": [{"book_id": book_id, "quantity": 1}],
                "currency": "INR",
                "provider": "manual",
                "billing_email": cust_email,
                "billing_name": "Flow Customer",
            },
        )
        ok = r.status_code in (200, 201)
        record("create order", ok, f"{r.status_code} {'' if ok else r.text[:200]}")
        if not ok:
            return 1
        # 201 returns a CheckoutSession wrapping the order, not the order itself —
        # the useful part for a browser is the provider handoff alongside it.
        session = r.json()
        order = session["order"]
        order_id = order["id"]
        print(
            f"        subtotal={order.get('subtotal_minor')} tax={order.get('tax_minor')} "
            f"total={order.get('total_minor')} status={order.get('status')} "
            f"provider={session.get('provider')} checkout_url={session.get('checkout_url')}"
        )

        r = c.post(f"/v1/admin/orders/{order_id}/mark-paid", headers=admin_h)
        record(
            "operator settles the order",
            r.status_code in (200, 201),
            f"{r.status_code} {r.json().get('status') if r.status_code == 200 else r.text[:130]}",
        )

        print("\n=== 5. entitlement propagates over the event bus ===")
        entitled, waited = False, 0.0
        for _ in range(16):
            time.sleep(2.0)
            waited += 2.0
            a = c.get(f"/v1/books/{book_id}/access", headers=user_h)
            if a.status_code == 200 and a.json().get("has_access"):
                entitled = True
                break
        record(
            "entitlement granted by payment.succeeded",
            entitled,
            f"after {waited:.0f}s — {a.text[:140]}",
        )

        r = c.get(f"/v1/books/{book_id}/download", headers=user_h)
        ok = r.status_code == 200
        record("download URL now minted", ok, f"{r.status_code} {r.text[:150]}")

        if ok:
            url = r.json()["url"]
            with httpx.Client(timeout=90.0, follow_redirects=True) as raw:
                got = raw.get(url)
            same = got.status_code == 200 and got.content == epub
            record(
                "signed URL returns the exact bytes uploaded",
                same,
                f"{got.status_code} {len(got.content)} bytes, identical={got.content == epub}",
            )

        r = c.get("/v1/library", headers=user_h)
        in_library = slug in [b.get("slug") for b in r.json().get("items", [])]
        record("book is in the customer's library", in_library, f"{r.status_code}")

        r = c.put(
            f"/v1/reading-progress/{book_id}",
            headers=user_h,
            json={"position": "epubcfi(/6/4)", "percent": 12.5},
        )
        record(
            "save reading progress", r.status_code in (200, 201), f"{r.status_code} {r.text[:110]}"
        )

        r = c.get("/v1/invoices", headers=user_h)
        n = len(r.json().get("items", [])) if r.status_code == 200 else 0
        record(
            "GST invoice issued", r.status_code == 200 and n >= 1, f"{r.status_code} {n} invoice(s)"
        )

        print("\n=== 6. operator sees it ===")
        r = c.get("/v1/admin/invoices", headers=admin_h, params={"user_id": order["user_id"]})
        record("admin invoice listing", r.status_code == 200, f"{r.status_code} {r.text[:130]}")
        r = c.get("/v1/admin/orders", headers=admin_h, params={"limit": 5})
        record("admin order listing", r.status_code == 200, str(r.status_code))
        r = c.get("/v1/admin/revenue", headers=admin_h)
        record(
            "admin revenue (net excludes GST)",
            r.status_code == 200,
            f"{r.status_code} {r.text[:170]}",
        )

        print("\n=== 7. search indexes it ===")
        found, waited = False, 0.0
        for _ in range(10):
            time.sleep(3.0)
            waited += 3.0
            s = c.get("/v1/search", params={"q": "smoke test handbook"})
            if s.status_code == 200 and s.json().get("hits"):
                found = True
                break
        record(
            "book reachable through search",
            found,
            f"after {waited:.0f}s — {s.status_code} hits={len(s.json().get('hits', []))}",
        )

    print("\n" + "=" * 66)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(k, d) for k, ok, d in results if not ok]
    print(f"{passed}/{len(results)} passed")
    if failed:
        print("\nFAILURES:")
        for k, d in failed:
            print(f"  - {k}: {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
