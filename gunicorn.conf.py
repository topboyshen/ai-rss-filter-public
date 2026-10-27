# Gunicorn 配置文件
# 用法: gunicorn -c gunicorn.conf.py app:app
import os


def _bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


bind = "0.0.0.0:5000"
# The API serves only bounded HTTP requests. The Glance widget is a short-lived
# SQLite-backed response rather than a streaming connection.
workers = _bounded_int("WEB_CONCURRENCY", 1, 1, 4)
worker_class = "gthread"
# Rate limits and lightweight caches are process-local, so production keeps a
# single worker unless an operator explicitly accepts those semantics and
# raises WEB_CONCURRENCY.
threads = _bounded_int("GUNICORN_THREADS", 16, 4, 64)
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"


def post_worker_init(worker):
    """Fail closed before accepting protected API traffic.

    All recurring/background jobs belong to ``ai-filter-worker`` in production.
    """
    from routes.shared import _security_cfg
    from services.shared_cache import process_role

    if not (_security_cfg().get("api_key") or "").strip():
        raise RuntimeError(
            "Effective security.api_key is required before worker threads start"
        )

    # app:app remains the deployment entry point, but importing it must be
    # side-effect free so test collection and tooling cannot mutate SQLite.
    # Gunicorn invokes this hook after the worker has loaded the application
    # and before it accepts traffic, which gives database initialization an
    # explicit production startup boundary.
    from app import initialize_app
    from services import production_startup
    if production_startup.enabled():
        slot, attempt = worker.production_slot
        initialize_app(slot=slot, attempt=attempt)
        return
    initialize_app()

    role = process_role()
    if role == "web":
        return
    if role != "all":
        raise RuntimeError("AI_FILTER_ROLE=web is required for the web service")

    # role=all is kept for local development. Production recurring jobs remain
    # isolated in the worker or host scheduler.
    from routes.dashboard_routes import _ensure_quotes_refresher
    _ensure_quotes_refresher()
    from routes.webhook_routes import start_webhook_dispatcher
    start_webhook_dispatcher()


def when_ready(server):
    from services import production_startup
    if production_startup.enabled():
        production_startup.install_gunicorn(server)


def pre_fork(server, worker):
    from services import production_startup
    if production_startup.enabled():
        production_startup.pre_fork(server, worker)
