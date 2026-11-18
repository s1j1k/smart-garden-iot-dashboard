"""Entry point: the dashboard (app.py) served as an installable web app.

    streamlit run src/server.py --server.address 0.0.0.0 --server.port 8501

``streamlit run`` sees the ``st.App`` below and serves app.py with the PWA routes
(manifest, service worker, icons) and head tags from ``pwa.py``. With
``SMART_GARDEN_SITE=admin`` every route is behind the login in ``admin_auth.py``.
"""

import streamlit as st
from starlette.middleware import Middleware

import admin_auth
from load_guard import LoadGuardMiddleware, StaticCacheMiddleware
from pwa import ROUTES, PwaHeadMiddleware

routes, middleware = list(ROUTES), [Middleware(StaticCacheMiddleware), Middleware(PwaHeadMiddleware)]
if admin_auth.is_admin_site():
    routes += admin_auth.ROUTES
    middleware.insert(0, Middleware(admin_auth.AdminAuthMiddleware))
else:
    # Public site only: under a crowd new visitors get a "high load" page, so the
    # Pi stays responsive and the admin site (its own process) keeps working.
    middleware.insert(0, Middleware(LoadGuardMiddleware))

app = st.App("app.py", routes=routes, middleware=middleware)
